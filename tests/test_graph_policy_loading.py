"""Read-only snapshot lifecycle checks; invented data, no API or quality score."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from support_agent import policy as graph
from support_agent import knowledge_release as release
from support_agent.service import create_server
from support_agent.policy import PublicPolicyConfig
from tests.test_knowledge_release import release_fixture


class GraphPolicyLoadingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.inputs = release_fixture(self.root)
        self.store = self.root / "store"
        self.config = self.build_config("first", 0)
        with graph._KNOWLEDGE_LOCK:
            graph._KNOWLEDGE_CACHE.clear()

    def build_config(self, label, generation):
        identifier = release.build_release(self.store, *self.inputs, label=label,
            created_at="2026-09-20T01:00:00Z")["release_id"]
        pointer = release.activate_release(self.store, identifier, expected_generation=generation,
                                          reason="Invented loader lifecycle fixture")
        directory = self.store / "releases" / identifier
        return PublicPolicyConfig(directory / "corpus/corpus.json", directory / "structure.json",
            directory / "relations.json", knowledge_store=self.store,
            knowledge_release={key: pointer[key] for key in ("release_id", "manifest_sha256", "generation", "channel")})

    def test_concurrent_first_load_builds_once_and_all_callers_share_snapshot(self):
        with patch.object(release, "PolicyEvidenceBundles", wraps=release.PolicyEvidenceBundles) as build:
            with ThreadPoolExecutor(max_workers=3) as pool:
                snapshots = list(pool.map(lambda _: graph.load_policy_knowledge(self.config), range(3)))
        self.assertEqual(build.call_count, 1)
        self.assertTrue(all(snapshot is snapshots[0] for snapshot in snapshots))
        with patch.object(release, "PolicyEvidenceBundles", side_effect=AssertionError("warm load rebuilt")):
            again = graph.load_policy_knowledge(replace(self.config))
        self.assertIs(again, snapshots[0])

    def test_active_resolver_prewarms_snapshot_without_graph_runtime(self):
        with patch.object(release, "PolicyEvidenceBundles", wraps=release.PolicyEvidenceBundles) as build:
            config = graph.resolve_policy_config(self.store)
            self.assertEqual(build.call_count, 1)
            snapshot = graph.load_policy_knowledge(config)
            again = graph.resolve_policy_config(self.store)
            self.assertEqual(build.call_count, 1)
        self.assertEqual(config.release_metadata(), self.config.release_metadata())
        self.assertIs(graph.load_policy_knowledge(again), snapshot)

    @unittest.skipUnless(importlib.util.find_spec("pydantic") is not None,
                         "install the optional graph dependencies for the Graph HTTP service")
    def test_active_resolver_prewarms_once_and_service_does_not_construct_legacy_backend(self):
        servers = []
        try:
            with patch.object(release, "PolicyEvidenceBundles", wraps=release.PolicyEvidenceBundles) as build:
                config = graph.resolve_policy_config(self.store)
                for number in range(2):
                    servers.append(create_server(db_path=self.root / f"server-{number}.sqlite3",
                        credentials={"synthetic-local-token": "alice"}, port=0,
                        public_policy_config=config))
            self.assertEqual(build.call_count, 1)
            self.assertIs(servers[0].policy_knowledge, servers[1].policy_knowledge)
            self.assertNotEqual(servers[0].db_path, servers[1].db_path)
        finally:
            for server in servers:
                server.server_close()

    @unittest.skipUnless(importlib.util.find_spec("pydantic") is not None,
                         "install the optional graph dependencies for the Graph HTTP service")
    def test_app_startup_validates_the_release_once(self):
        # The policy part of `app` startup: pin the release, then create the server.
        from support_agent.cli import _release_policy_config
        with patch.object(release, "_validate_directory", wraps=release._validate_directory) as validate, \
                patch.object(release, "PolicyEvidenceBundles", wraps=release.PolicyEvidenceBundles) as build, \
                patch.object(graph, "PolicyEvidenceBundles", side_effect=AssertionError("startup rebuilt bundles")):
            config = _release_policy_config(self.store)
            server = create_server(db_path=self.root / "app.sqlite3", credentials={"synthetic-local-token": "alice"},
                                   port=0, public_policy_config=config)
        self.addCleanup(server.server_close)
        self.assertEqual(validate.call_count, 1)
        self.assertEqual(build.call_count, 1)
        self.assertIs(server.policy_knowledge, graph.load_policy_knowledge(config))
        self.assertEqual(config.release_metadata(), self.config.release_metadata())

    def test_route_lookups_reuse_the_snapshot_bundles_without_rebuilding(self):
        snapshot = graph.load_policy_knowledge(self.config)
        adapter = graph.PolicyAdapter(self.config, knowledge=snapshot)
        rebuilt = graph.PolicyAdapter(self.config, knowledge=replace(snapshot,
            bundles=graph.PolicyEvidenceBundles(self.config.corpus_path, self.config.structure_path,
                                                self.config.relations_path)))
        for origin, destination, status in (("甲", "乙", "evidence_found"), ("乙", "甲", "route_not_listed")):
            with self.subTest(route=(origin, destination)):
                with patch.object(graph, "PolicyEvidenceBundles", side_effect=AssertionError("route rebuilt bundles")):
                    reference = adapter.route_reference(origin, destination)
                self.assertEqual(reference["status"], status)
                self.assertEqual(reference, rebuilt.route_reference(origin, destination))

    def test_shared_snapshot_and_returned_evidence_cannot_change_other_sessions(self):
        snapshot = graph.load_policy_knowledge(self.config)
        adapters = [graph.PolicyAdapter(self.config, knowledge=snapshot) for _ in range(3)]
        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(lambda adapter: adapter.source_cards(["rule:responsibility"]), adapters))
        expected = results[1][0]["content"]
        results[0][0]["content"] = "another session changed this copy"
        self.assertEqual(results[1][0]["content"], expected)
        first = next(iter(snapshot.evidence.values()))
        with self.assertRaises(TypeError):
            first["text"] = "change shared data"
        with self.assertRaises(TypeError):
            first["spans"][0]["text"] = "change shared nested data"
        cards = adapters[1].source_cards(["rule:responsibility"])
        cards[0]["spans"][0]["text"] = "change detached card"
        fresh = adapters[2].source_cards(["rule:responsibility"])
        self.assertNotEqual(fresh[0]["spans"][0]["text"], "change detached card")

    def test_cache_hit_rejects_same_size_same_mtime_tamper_but_retained_snapshot_is_stable(self):
        snapshot = graph.load_policy_knowledge(self.config)
        adapter = graph.PolicyAdapter(self.config, knowledge=snapshot)
        original_result = adapter.source_cards(["rule:responsibility", "rule:conditions"])
        path = Path(self.config.relations_path)
        original, stat = path.read_bytes(), path.stat()
        changed = bytearray(original)
        changed[10] ^= 1
        try:
            path.write_bytes(changed)
            os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
            self.assertEqual(path.stat().st_size, stat.st_size)
            self.assertEqual(path.stat().st_mtime_ns, stat.st_mtime_ns)
            with self.assertRaisesRegex(ValueError, "ASSET_HASH_MISMATCH"):
                graph.load_policy_knowledge(self.config)
            self.assertEqual(adapter.source_cards(["rule:responsibility", "rule:conditions"]), original_result)
        finally:
            path.write_bytes(original)
        self.assertIs(graph.load_policy_knowledge(self.config), snapshot)

    def test_failed_first_load_does_not_publish_partial_snapshot_and_can_retry(self):
        path = Path(self.config.relations_path)
        original = path.read_bytes()
        try:
            path.write_bytes(original + b" ")
            with self.assertRaisesRegex(ValueError, "ASSET_HASH_MISMATCH"):
                graph.load_policy_knowledge(self.config)
            self.assertEqual(len(graph._KNOWLEDGE_CACHE), 0)
        finally:
            path.write_bytes(original)
        self.assertIsNotNone(graph.load_policy_knowledge(self.config))
        self.assertEqual(len(graph._KNOWLEDGE_CACHE), 1)

    def test_server_refuses_to_start_on_a_tampered_release_before_creating_storage(self):
        path = Path(self.config.relations_path)
        original = path.read_bytes()
        database = self.root / "refused.sqlite3"
        try:
            path.write_bytes(original + b" ")
            with self.assertRaisesRegex(ValueError, "ASSET_HASH_MISMATCH"):
                create_server(db_path=database, credentials={"local-unit-token": "alice"}, port=0,
                              public_policy_config=self.config)
            self.assertFalse(database.exists())
            self.assertEqual(len(graph._KNOWLEDGE_CACHE), 0)
        finally:
            path.write_bytes(original)

    def test_config_rejects_incomplete_invalid_or_overstated_release_binding(self):
        paths = (self.config.corpus_path, self.config.structure_path, self.config.relations_path)
        metadata = self.config.release_metadata()
        with self.assertRaisesRegex(ValueError, "知识发布绑定不完整"):
            PublicPolicyConfig(*paths, knowledge_store=self.store)
        invalid = [{**metadata, "generation": 0}, {**metadata, "channel": "staging"},
                   {key: value for key, value in metadata.items() if key != "release_id"},
                   {**metadata, "unexpected": "field"}]
        for bound in invalid:
            with self.subTest(bound=sorted(bound.items())):
                with self.assertRaisesRegex(ValueError, "知识发布元数据无效"):
                    PublicPolicyConfig(*paths, knowledge_store=self.store, knowledge_release=bound)
        with self.assertRaisesRegex(ValueError, "知识发布元数据无效"):
            PublicPolicyConfig(*paths, knowledge_release=metadata)
        for bound in ({**metadata, "review_status": "human_attestations_recorded"},
                      {**metadata, "effective_dates": "verified"}):
            with self.subTest(bound=sorted(bound.items())):
                with self.assertRaisesRegex(ValueError, "知识审核或生效状态声明无效"):
                    PublicPolicyConfig(*paths, knowledge_store=self.store, knowledge_release=bound)

    def test_new_release_is_separate_and_old_config_remains_pinned_after_activation(self):
        first = graph.load_policy_knowledge(self.config)
        second_config = self.build_config("second", 1)
        second = graph.load_policy_knowledge(second_config)
        self.assertIsNot(first, second)
        self.assertIs(graph.load_policy_knowledge(self.config), first)
        self.assertEqual(first.metadata["knowledge_release"]["generation"], 1)
        self.assertEqual(second.metadata["knowledge_release"]["generation"], 2)
        with self.assertRaisesRegex(ValueError, "KNOWLEDGE_BINDING_INVALID"):
            graph.PolicyAdapter(second_config, knowledge=first)

    def test_cache_key_cannot_bypass_generation_path_manifest_or_review_binding(self):
        graph.load_policy_knowledge(self.config)
        bad = [replace(self.config, corpus_path=self.inputs[0]),
               replace(self.config, knowledge_release={**self.config.release_metadata(), "generation": 99}),
               replace(self.config, knowledge_release={**self.config.release_metadata(), "manifest_sha256": "0" * 64}),
               replace(self.config, knowledge_release={**self.config.release_metadata(), "channel": "reviewed",
                   "review_status": "human_attestations_recorded"})]
        for config in bad:
            with self.subTest(config=config.release_metadata()):
                with self.assertRaises(ValueError):
                    graph.load_policy_knowledge(config)
        self.assertEqual(len(graph._KNOWLEDGE_CACHE), 1)


if __name__ == "__main__":
    unittest.main()
