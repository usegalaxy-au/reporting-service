"""Tests for common.state."""

import tempfile
import unittest
from datetime import date
from pathlib import Path

from common import state


class FakeS3:
    """Minimal S3Storage stub providing only date_from_key."""
    def __init__(self, prefix):
        self.prefix = prefix

    def date_from_key(self, key):
        # key looks like prefix + YYYY/MM/DD/file.gz
        date_part = key[len(self.prefix):len(self.prefix) + 10]
        return date.fromisoformat(date_part.replace('/', '-'))


class LoadIngestedKeysTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state_dir = Path(self._tmp.name)

    def test_returns_empty_set_when_no_files(self):
        result = state.load_ingested_keys(
            self.state_dir, date(2026, 6, 1), date(2026, 6, 7))
        self.assertEqual(result, set())

    def test_reads_keys_from_relevant_monthly_files(self):
        (self.state_dir / '2026-06').write_text('a\nb\nc\n')
        (self.state_dir / '2026-07').write_text('d\n')
        (self.state_dir / '2026-08').write_text('e\n')
        result = state.load_ingested_keys(
            self.state_dir, date(2026, 6, 30), date(2026, 7, 2))
        self.assertEqual(result, {'a', 'b', 'c', 'd'})

    def test_skips_blank_lines(self):
        (self.state_dir / '2026-06').write_text('a\n\n  \nb\n')
        result = state.load_ingested_keys(
            self.state_dir, date(2026, 6, 1), date(2026, 6, 1))
        self.assertEqual(result, {'a', 'b'})

    def test_single_day_reads_single_month(self):
        (self.state_dir / '2026-06').write_text('a\n')
        (self.state_dir / '2026-05').write_text('old\n')
        result = state.load_ingested_keys(
            self.state_dir, date(2026, 6, 15), date(2026, 6, 15))
        self.assertEqual(result, {'a'})


class MarkIngestedTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state_dir = Path(self._tmp.name) / 'sub' / 'state'
        self.s3 = FakeS3(prefix='nginx-logs/dev/tool_runs/')

    def test_appends_to_month_file_and_creates_parent_dirs(self):
        key = 'nginx-logs/dev/tool_runs/2026/06/29/abc.log.gz'
        state.mark_ingested(self.state_dir, self.s3, key)
        month_file = self.state_dir / '2026-06'
        self.assertTrue(month_file.exists())
        self.assertEqual(month_file.read_text(), key + '\n')

    def test_appends_multiple_keys(self):
        k1 = 'nginx-logs/dev/tool_runs/2026/06/29/a.log.gz'
        k2 = 'nginx-logs/dev/tool_runs/2026/06/29/b.log.gz'
        state.mark_ingested(self.state_dir, self.s3, k1)
        state.mark_ingested(self.state_dir, self.s3, k2)
        lines = (self.state_dir / '2026-06').read_text().splitlines()
        self.assertEqual(lines, [k1, k2])

    def test_writes_to_correct_month_file(self):
        k_june = 'nginx-logs/dev/tool_runs/2026/06/30/a.log.gz'
        k_july = 'nginx-logs/dev/tool_runs/2026/07/01/b.log.gz'
        state.mark_ingested(self.state_dir, self.s3, k_june)
        state.mark_ingested(self.state_dir, self.s3, k_july)
        self.assertEqual(
            (self.state_dir / '2026-06').read_text(), k_june + '\n')
        self.assertEqual(
            (self.state_dir / '2026-07').read_text(), k_july + '\n')


class RoundTripTests(unittest.TestCase):
    def test_marked_keys_load_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            s3 = FakeS3(prefix='p/')
            keys = [
                'p/2026/06/29/a.log.gz',
                'p/2026/06/30/b.log.gz',
                'p/2026/07/01/c.log.gz',
            ]
            for k in keys:
                state.mark_ingested(state_dir, s3, k)
            loaded = state.load_ingested_keys(
                state_dir, date(2026, 6, 1), date(2026, 7, 31))
            self.assertEqual(loaded, set(keys))


if __name__ == '__main__':
    unittest.main()
