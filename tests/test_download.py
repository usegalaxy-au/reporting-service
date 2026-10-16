import gzip
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

import download

PREFIX = 'galaxy/nginx-logs/cleaned/'


class FakeS3:
    """S3Storage stub serving gzipped bodies keyed by day."""

    def __init__(self, lines_by_key):
        self.prefix = PREFIX
        self._bodies = {
            k: gzip.compress(v.encode()) for k, v in lines_by_key.items()
        }

    def iter_keys(self, start, end):
        for key in sorted(self._bodies):
            if start <= self.date_from_key(key) <= end:
                yield key

    def date_from_key(self, key):
        d = key[len(self.prefix):len(self.prefix) + 10]
        return date.fromisoformat(d.replace('/', '-'))

    def read_object(self, key):
        return self._bodies[key]


def key(day, name):
    return f'{PREFIX}{day:%Y/%m/%d}/{name}.log.gz'


class BatchWindowTests(unittest.TestCase):
    def test_clips_last_window(self):
        windows = download.batch_windows(
            date(2026, 9, 1), date(2026, 10, 15), 7)
        self.assertEqual(len(windows), 7)
        self.assertEqual(windows[0], (date(2026, 9, 1), date(2026, 9, 7)))
        self.assertEqual(
            windows[-1], (date(2026, 10, 13), date(2026, 10, 15)))

    def test_single_day_windows(self):
        windows = download.batch_windows(date(2026, 9, 1), date(2026, 9, 3), 1)
        self.assertEqual(len(windows), 3)

    def test_build_prefix(self):
        self.assertEqual(
            download.build_prefix('prod', 'cleaned'), PREFIX)
        self.assertEqual(
            download.build_prefix('dev', 'raw'), 'dev/nginx-logs/raw/')


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.outdir = Path(self._tmp.name) / 'out'
        d1 = date(2026, 9, 1)
        self.s3 = FakeS3({
            key(d1, 'a'): 'a\n',
            key(d1, 'b'): 'b\n',
            key(d1 + timedelta(days=2), 'c'): 'c\n',
            key(d1 + timedelta(days=8), 'd'): 'd\n',
        })
        self.start = d1
        self.end = d1 + timedelta(days=10)

    def test_individual_files(self):
        stats = download.download(
            self.s3, self.start, self.end, self.outdir, decompress=True)
        self.assertEqual(stats['objects'], 4)
        self.assertEqual(
            (self.outdir / '2026-09-01_a.log').read_text(), 'a\n')
        self.assertEqual(len(list(self.outdir.iterdir())), 4)

    def test_squash_decompress(self):
        stats = download.download(
            self.s3, self.start, self.end, self.outdir,
            decompress=True, squash_days=7)
        self.assertEqual(stats['files'], 2)
        self.assertEqual(
            (self.outdir / '2026-09-01_2026-09-07.log').read_text(),
            'a\nb\nc\n')
        self.assertEqual(
            (self.outdir / '2026-09-08_2026-09-11.log').read_text(), 'd\n')

    def test_squash_without_decompress_is_valid_gzip(self):
        download.download(
            self.s3, self.start, self.end, self.outdir, squash_days=7)
        path = self.outdir / '2026-09-01_2026-09-07.log.gz'
        self.assertEqual(gzip.decompress(path.read_bytes()), b'a\nb\nc\n')

    def test_rerun_rebuilds_batches(self):
        for _ in range(2):
            download.download(
                self.s3, self.start, self.end, self.outdir,
                decompress=True, squash_days=7)
        self.assertEqual(
            (self.outdir / '2026-09-01_2026-09-07.log').read_text(),
            'a\nb\nc\n')

    def test_empty_windows_write_nothing(self):
        download.download(
            self.s3, self.start, self.start + timedelta(days=30),
            self.outdir, decompress=True, squash_days=3)
        names = sorted(p.name for p in self.outdir.iterdir())
        self.assertEqual(
            names, ['2026-09-01_2026-09-03.log', '2026-09-07_2026-09-09.log'])


if __name__ == '__main__':
    unittest.main()
