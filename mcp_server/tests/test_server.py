import asyncio
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from health_mcp import app, hashing


class PassphraseTests(unittest.TestCase):
    def test_hash_round_trip_and_rejection(self):
        stored = hashing.hash_passphrase('correct horse battery staple', n=2**10)
        self.assertTrue(hashing.verify_passphrase('correct horse battery staple', stored))
        self.assertFalse(hashing.verify_passphrase('wrong', stored))
        self.assertFalse(hashing.verify_passphrase('anything', 'not-a-hash'))

    def test_unknown_users_are_checked_against_a_decoy(self):
        self.assertTrue(hashing.DECOY.startswith('scrypt$'))
        self.assertFalse(hashing.verify_passphrase('', hashing.DECOY))


class ScopeTests(unittest.TestCase):
    def test_write_tools_require_write_scope(self):
        token = mock.Mock(scopes=['health.read'])
        with mock.patch.object(app, 'get_access_token', return_value=token):
            with self.assertRaisesRegex(PermissionError, 'health.write'):
                app._require_write()
        with mock.patch.object(app, 'get_access_token', return_value=mock.Mock(scopes=['health.read', 'health.write'])):
            app._require_write()

    def test_safe_names(self):
        self.assertEqual(app._safe_name('2026-10-24 weight/bp'), '2026-10-24-weight-bp')
        with self.assertRaises(ValueError):
            app._safe_name('../..')

    def test_tools_are_registered_with_annotations(self):
        server = app.build()
        tools = {t.name: t for t in asyncio.run(server.list_tools())}
        self.assertTrue({'search', 'fetch', 'record_narrative', 'record_medication', 'capture_conversation'} <= set(tools))
        self.assertTrue(tools['search'].annotations.read_only_hint)
        self.assertFalse(tools['record_narrative'].annotations.read_only_hint)


if __name__ == '__main__':
    unittest.main()
