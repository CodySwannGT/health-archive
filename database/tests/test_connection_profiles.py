"""A missing or insecure remote profile must never select stale local data."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from common import connection_settings


class ConnectionProfiles(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.config = self.root / 'connection.json'
        self.env = patch.dict(os.environ, {'HEALTH_DB_CONFIG': str(self.config)}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def write(self, profile):
        self.config.write_text(json.dumps({'default': 'railway', 'profiles': {'railway': profile}}))

    def test_missing_remote_profile_fails_instead_of_using_local(self):
        with patch.dict(os.environ, {'HEALTH_DB_TARGET': 'railway'}):
            with self.assertRaisesRegex(RuntimeError, 'missing'):
                connection_settings()

    def test_remote_requires_hostname_and_certificate_verification(self):
        for mode in ['disable', 'allow', 'prefer', 'require', 'verify-ca']:
            self.write({'reader': {'sslmode': mode, 'sslrootcert': 'ca.crt'}})
            with self.assertRaisesRegex(RuntimeError, 'verified TLS'):
                connection_settings()

    def test_remote_requires_existing_trust_certificate(self):
        self.write({'reader': {'sslmode': 'verify-full', 'sslrootcert': 'missing.crt'}})
        with self.assertRaisesRegex(RuntimeError, 'certificate is missing'):
            connection_settings()

    def test_role_and_database_are_selected_explicitly(self):
        cert = self.root / 'ca.crt'
        cert.write_text('test certificate')
        base = {'host': 'example.invalid', 'port': 5432,
                'sslmode': 'verify-full', 'sslrootcert': 'ca.crt'}
        self.write({'admin': dict(base, user='health_admin', password='admin'),
                    'reader': dict(base, user='health_reader', password='reader')})
        reader = connection_settings(database='test_restore')
        self.assertEqual(reader['user'], 'health_reader')
        self.assertEqual(reader['dbname'], 'test_restore')
        self.assertEqual(reader['sslrootcert'], str(cert))
        self.assertEqual(connection_settings(admin=True)['user'], 'health_admin')


if __name__ == '__main__':
    unittest.main()
