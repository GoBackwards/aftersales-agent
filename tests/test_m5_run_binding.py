"""Immutable run metadata survives cancellation and restart; no API calls."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from support_agent.sessions import SessionError, SessionStore


class RunKnowledgeBindingTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / 'db.sqlite3'
        self.store = SessionStore(self.path)
        self.addCleanup(lambda: self.store.close())
        self.sid = self.store.create_session('alice')['session_id']
        self.run = self.store.begin_turn('alice', self.sid, 'r1', '政策问题', mode='offline_scripted')['run_id']
        self.release = {'release_id': 'a' * 64, 'manifest_sha256': 'a' * 64, 'generation': 1,
            'channel': 'development', 'review_status': 'development_candidate', 'effective_dates': 'unverified_snapshot'}

    def bind(self, release=None):
        self.store.record_run_event('alice', self.sid, self.run, 'run_context',
            {'knowledge_release': self.release if release is None else release, 'conversation_version': 'm5'})

    def test_cancellation_and_replay_after_restart_preserve_original_release(self):
        self.bind()
        self.store.cancel('alice', self.sid, 'cancel1')
        self.store.close()
        self.store = SessionStore(self.path)
        result = self.store.begin_turn('alice', self.sid, 'r1', '政策问题')['response']
        self.assertEqual(result['status'], 'cancelled')
        self.assertEqual(result['knowledge_release'], self.release)
        self.assertEqual(result['conversation_version'], 'm5')

    def test_binding_cannot_change_within_run(self):
        self.bind()
        other = {**self.release, 'release_id': 'b' * 64, 'manifest_sha256': 'b' * 64}
        with self.assertRaises(SessionError):
            self.bind(other)
        self.store.fail_turn('alice', self.sid, self.run, 'test failure')
        result = self.store.begin_turn('alice', self.sid, 'r1', '政策问题')['response']
        self.assertEqual(result['knowledge_release'], self.release)

    def test_foreign_identity_and_cancelled_lease_cannot_bind(self):
        with self.assertRaises(SessionError):
            self.store.record_run_event('bob', self.sid, self.run, 'run_context', {'knowledge_release': self.release})
        self.store.cancel('alice', self.sid, 'cancel1')
        with self.assertRaises(SessionError):
            self.bind()

    def test_unsafe_metadata_and_false_review_claims_are_rejected(self):
        for changed in ({'url': 'https://invalid.example/secret'}, {'review_status': 'human_attestations_recorded'},
                        {'generation': True}, {'manifest_sha256': 'z' * 64}, {'release_id': 'b' * 64},
                        {'effective_dates': 'verified'}):
            with self.subTest(changed=changed), self.assertRaises(SessionError):
                self.bind({**self.release, **changed})
        self.assertFalse(any(e['kind'] == 'run_context' for e in self.store.get_run('alice', self.sid, self.run)['events']))


if __name__ == '__main__':
    unittest.main()
