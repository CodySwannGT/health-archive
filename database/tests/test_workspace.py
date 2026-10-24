import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
import common
import workspace


def sha(content):
    return hashlib.sha256(content).hexdigest()


class FakeConnection:
    """Minimal stand-in for the artifact tables the guards query."""
    def __init__(self, versions, contents, retired=()):
        self.versions, self.contents, self.retired = versions, contents, set(retired)

    def execute(self, query, params):
        # Archive queries pass (patient, path); content lookups pass the hash.
        if 'retired_run IS NOT NULL' in query:
            return Result({'x': 1} if params[-1] in self.retired else None)
        if 'artifact_versions' in query:
            return [{'sha256': s} for s in self.versions.get(params[-1], ())]
        return Result({'bytes': self.contents[params[0]]})


class Result(list):
    def __init__(self, row):
        super().__init__([row]);self.row=row

    def fetchone(self):
        return self.row


def jsonl(*ids):
    return ''.join(json.dumps({'record_id': i}) + '\n' for i in ids).encode()


class PutGuardTests(unittest.TestCase):
    def setUp(self):
        self.account = common.use_account(common.Account('usr_test', 'pat_test'))
        self.cache = tempfile.TemporaryDirectory()
        self.original = workspace.CACHE
        workspace.CACHE = Path(self.cache.name)

    def tearDown(self):
        common.reset_account(self.account)
        workspace.CACHE = self.original
        self.cache.cleanup()

    def test_stale_version_is_refused(self):
        old, new = jsonl('a'), jsonl('a', 'b')
        conn = FakeConnection({'symptoms/records.jsonl': {sha(old), sha(new)}}, {sha(new): new})
        with self.assertRaisesRegex(workspace.StaleSourceError, 'older version'):
            workspace.check_put(conn, 'symptoms/records.jsonl', old, sha(new))

    def test_record_dropping_rewrite_is_refused(self):
        current, divergent = jsonl('a', 'b'), jsonl('a', 'c')
        conn = FakeConnection({}, {sha(current): current})
        with self.assertRaisesRegex(workspace.StaleSourceError, 'drop 1 records'):
            workspace.check_put(conn, 'symptoms/records.jsonl', divergent, sha(current))

    def test_additive_and_unchanged_versions_are_accepted(self):
        current, grown = jsonl('a'), jsonl('a', 'b')
        conn = FakeConnection({}, {sha(current): current})
        self.assertEqual(workspace.check_put(conn, 'symptoms/records.jsonl', grown, sha(current)), 'changed')
        self.assertEqual(workspace.check_put(conn, 'symptoms/records.jsonl', current, sha(current)), 'unchanged')
        self.assertEqual(workspace.check_put(conn, 'lab-results/new.json', b'{}', None), 'new')

    def test_retired_file_is_restored_only_by_explicit_put(self):
        content = b'note'
        conn = FakeConnection({'research/n.md': {sha(content)}}, {}, retired={'research/n.md'})
        with self.assertRaisesRegex(workspace.StaleSourceError, 'retired'):
            workspace.check_put(conn, 'research/n.md', content, None)
        self.assertEqual(workspace.check_put(conn, 'research/n.md', content, None, restore=True), 'restored')

    def test_archive_membership_excludes_code(self):
        self.assertTrue(workspace.in_archive('symptoms/records.jsonl'))
        self.assertTrue(workspace.in_archive('ontology/relations.json'))
        self.assertFalse(workspace.in_archive('symptoms/tools/ingest-narrative.py'))
        self.assertFalse(workspace.in_archive('database/tools/ingest.py'))


if __name__ == '__main__':
    unittest.main()
