import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from botocore.exceptions import ReadTimeoutError

import s3_cleanup
from common import s3 as s3_module
from common.s3 import S3Storage

ENDPOINT = 'https://object-store.example.org'


def make_keys(n):
    return [f'galaxy/nginx-logs/raw/2020/01/01/{i:06d}.log.gz'
            for i in range(n)]


class FakeClient:
    """boto3 client stub; ``outcomes`` is one entry per delete call."""

    def __init__(self, outcomes=None):
        self.calls = []
        self.outcomes = list(outcomes or [])

    def delete_objects(self, Bucket, Delete):
        self.calls.append(Delete)
        outcome = self.outcomes.pop(0) if self.outcomes else {}
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def storage_with(client):
    storage = S3Storage()
    storage._client = client
    return storage


class DeleteObjectsTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict('os.environ', {'S3_BUCKET': 'bucket'})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_deletes_in_batches(self):
        client = FakeClient()
        deleted, failed = storage_with(client).delete_objects(
            make_keys(450), batch_size=200)
        self.assertEqual((deleted, failed), (450, 0))
        self.assertEqual(
            [len(c['Objects']) for c in client.calls], [200, 200, 50])
        self.assertTrue(all(c['Quiet'] for c in client.calls))

    def test_counts_per_key_errors(self):
        errors = {'Errors': [
            {'Key': 'a', 'Code': 'AccessDenied', 'Message': 'no'},
            {'Key': 'b', 'Code': 'InternalError', 'Message': 'oops'},
        ]}
        client = FakeClient([errors])
        deleted, failed = storage_with(client).delete_objects(
            make_keys(10), batch_size=200)
        self.assertEqual((deleted, failed), (8, 2))

    def test_continues_after_a_failed_batch(self):
        timeout = ReadTimeoutError(endpoint_url=ENDPOINT)
        client = FakeClient([{}, timeout, {}])
        deleted, failed = storage_with(client).delete_objects(
            make_keys(30), batch_size=10)
        self.assertEqual((deleted, failed), (20, 10))
        self.assertEqual(len(client.calls), 3)

    def test_gives_up_after_consecutive_failures(self):
        limit = s3_module.DELETE_MAX_CONSECUTIVE_FAILURES
        timeout = ReadTimeoutError(endpoint_url=ENDPOINT)
        client = FakeClient([timeout] * (limit + 5))
        keys = make_keys((limit + 5) * 10)
        deleted, failed = storage_with(client).delete_objects(
            keys, batch_size=10)
        self.assertEqual(len(client.calls), limit)
        self.assertEqual((deleted, failed), (0, len(keys)))


class CleanupMainTests(unittest.TestCase):
    def run_main(self, delete_result):
        storage = mock.Mock()
        storage.list_all_objects.return_value = [
            {'Key': key} for key in make_keys(3)]
        storage.delete_objects.return_value = delete_result
        with tempfile.TemporaryDirectory() as tmp:
            policy = Path(tmp) / 'policy.json'
            policy.write_text(json.dumps({'raw': 30, 'other': None}))
            argv = ['s3_cleanup.py', '--policy', str(policy)]
            with mock.patch.object(s3_cleanup, 'S3Storage',
                                   return_value=storage), \
                    mock.patch('sys.argv', argv):
                s3_cleanup.main()
        return storage

    def test_exits_zero_when_all_deleted(self):
        storage = self.run_main((3, 0))
        storage.delete_objects.assert_called_once()

    def test_exits_non_zero_when_any_failed(self):
        with self.assertRaises(SystemExit) as ctx:
            self.run_main((1, 2))
        self.assertEqual(ctx.exception.code, 1)


if __name__ == '__main__':
    unittest.main()
