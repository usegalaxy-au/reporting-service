"""Tests for common.runner."""

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date
from pathlib import Path
from unittest import mock

from common import runner


class FakeS3:
    def __init__(self, prefix=None, keys_by_day=None, records_by_key=None):
        self.prefix = prefix or 'p/'
        self._keys_by_day = keys_by_day or {}
        self._records_by_key = records_by_key or {}

    def iter_keys(self, start, end):
        current = start
        while current <= end:
            for k in self._keys_by_day.get(current, []):
                yield k
            from datetime import timedelta
            current += timedelta(days=1)

    def read_records(self, key):
        yield from self._records_by_key.get(key, [])

    def date_from_key(self, key):
        # keys here are short labels; map them by lookup
        for d, keys in self._keys_by_day.items():
            if key in keys:
                return d
        raise KeyError(key)


def make_report(state_dir, fake_s3, parse=None, build=None):
    """Build a Report wired to a fake S3 and recorded callbacks."""
    parse = parse or (lambda r: r if r else None)
    build = build or (lambda **kw: ['line:' + str(kw['parsed'])])
    return runner.Report(
        name='test',
        s3_prefix_env='TEST_S3_PREFIX',
        measurement='m',
        state_dir=state_dir,
        parse_record=parse,
        build_points=build,
    )


class ReportDataclassTests(unittest.TestCase):
    def test_fields_round_trip(self):
        r = runner.Report(
            name='x',
            s3_prefix_env='X_PREFIX',
            measurement='m',
            state_dir=Path('/tmp/x'),
            parse_record=lambda r: None,
            build_points=lambda **kw: [],
        )
        self.assertEqual(r.name, 'x')
        self.assertEqual(r.measurement, 'm')


class RunTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state_dir = Path(self._tmp.name)

        self.env_patch = mock.patch.dict(
            'os.environ',
            {
                'TEST_S3_PREFIX': 'p/',
                'GALAXY_DATABASE_URL': 'postgresql://fake/db',
            },
        )
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

        # create_engine + its returned connection context manager
        self.engine_patch = mock.patch('common.runner.create_engine')
        self.mock_create_engine = self.engine_patch.start()
        self.addCleanup(self.engine_patch.stop)
        self.mock_conn = mock.MagicMock(name='conn')
        engine = self.mock_create_engine.return_value
        engine.connect.return_value.__enter__.return_value = self.mock_conn
        engine.connect.return_value.__exit__.return_value = False

        # Domain map loader
        self.domains_patch = mock.patch(
            'common.runner.load_domain_map', return_value={'@a.com': 'A'})
        self.mock_load_domain = self.domains_patch.start()
        self.addCleanup(self.domains_patch.stop)

        # Influx writer
        self.write_patch = mock.patch('common.runner.write_to_influxdb')
        self.mock_write = self.write_patch.start()
        self.addCleanup(self.write_patch.stop)

    def _run_with(self, fake_s3, dry=False, **kw):
        with mock.patch('common.runner.S3Storage', return_value=fake_s3):
            report = make_report(self.state_dir, fake_s3, **kw)
            runner.run(
                report,
                start_date=date(2026, 6, 29),
                end_date=date(2026, 6, 29),
                dry=dry,
            )
            return report

    def test_writes_and_marks_ingested_when_points_emitted(self):
        s3 = FakeS3(
            keys_by_day={date(2026, 6, 29): ['k1']},
            records_by_key={'k1': [{'a': 1}, {'a': 2}]},
        )
        self._run_with(s3)

        self.mock_write.assert_called_once()
        sent = self.mock_write.call_args[0][0]
        self.assertEqual(len(sent), 2)
        # Marked ingested -> month file contains the key.
        self.assertEqual(
            (self.state_dir / '2026-06').read_text(),
            'k1\n',
        )

    def test_skips_already_ingested_keys(self):
        (self.state_dir / '2026-06').write_text('k1\n')
        read_calls = []

        def parse(r):
            read_calls.append(r)
            return r

        s3 = FakeS3(
            keys_by_day={date(2026, 6, 29): ['k1']},
            records_by_key={'k1': [{'a': 1}]},
        )
        self._run_with(s3, parse=parse)

        self.assertEqual(read_calls, [])
        self.mock_write.assert_not_called()

    def test_skips_when_parse_returns_none(self):
        s3 = FakeS3(
            keys_by_day={date(2026, 6, 29): ['k1']},
            records_by_key={'k1': [None, None]},
        )
        self._run_with(s3, parse=lambda r: r)  # None -> falsy -> skipped

        self.mock_write.assert_not_called()
        # Key not marked because no points emitted.
        self.assertFalse((self.state_dir / '2026-06').exists())

    def test_dry_run_prints_lines_and_skips_write_and_mark(self):
        s3 = FakeS3(
            keys_by_day={date(2026, 6, 29): ['k1']},
            records_by_key={'k1': [{'a': 1}]},
        )
        buf = io.StringIO()
        with redirect_stdout(buf):
            self._run_with(s3, dry=True)

        self.mock_write.assert_not_called()
        self.assertFalse((self.state_dir / '2026-06').exists())
        self.assertIn("line:{'a': 1}", buf.getvalue())

    def test_build_points_receives_expected_kwargs(self):
        captured = {}

        def build(**kw):
            captured.update(kw)
            return ['line']

        s3 = FakeS3(
            keys_by_day={date(2026, 6, 29): ['k1']},
            records_by_key={'k1': [{'a': 1}]},
        )
        self._run_with(s3, build=build)

        self.assertIs(captured['conn'], self.mock_conn)
        self.assertEqual(captured['parsed'], {'a': 1})
        self.assertEqual(captured['domain_map'], {'@a.com': 'A'})
        self.assertEqual(captured['measurement'], 'm')

    def test_key_with_zero_points_is_not_marked(self):
        s3 = FakeS3(
            keys_by_day={date(2026, 6, 29): ['k1']},
            records_by_key={'k1': []},
        )
        self._run_with(s3)
        self.mock_write.assert_not_called()
        self.assertFalse((self.state_dir / '2026-06').exists())


if __name__ == '__main__':
    unittest.main()
