"""Tests for common.influx."""

import unittest
import urllib.error
from datetime import datetime, timezone
from unittest import mock

from common import influx


class EscapeTagValueTests(unittest.TestCase):
    def test_passes_plain_value_through(self):
        self.assertEqual(influx.escape_tag_value('plain'), 'plain')

    def test_escapes_space_comma_equals_backslash(self):
        self.assertEqual(
            influx.escape_tag_value('a b,c=d\\e'),
            'a\\ b\\,c\\=d\\\\e',
        )

    def test_backslash_escaped_before_other_specials(self):
        # The escape order matters — backslash must be doubled first so a
        # backslash inserted by a later substitution isn't doubled again.
        self.assertEqual(influx.escape_tag_value('\\'), '\\\\')


class EscapeFieldStringTests(unittest.TestCase):
    def test_escapes_backslash_and_quote(self):
        self.assertEqual(
            influx.escape_field_string('a"b\\c'),
            'a\\"b\\\\c',
        )

    def test_does_not_escape_spaces_or_commas(self):
        # Field strings live inside quotes; only \ and " need escaping.
        self.assertEqual(
            influx.escape_field_string('a b,c=d'),
            'a b,c=d',
        )


class FormatLineProtocolTests(unittest.TestCase):
    def setUp(self):
        self.ts = datetime(2026, 6, 29, 12, 0, 0, tzinfo=timezone.utc)
        self.ts_epoch = int(self.ts.timestamp())

    def test_renders_tags_fields_and_timestamp(self):
        line = influx.format_line_protocol(
            measurement='m',
            tags={'a': 'x', 'b': 'y'},
            fields={'count': 1.0},
            timestamp=self.ts,
        )
        self.assertEqual(line, f'm,a=x,b=y count=1.0 {self.ts_epoch}')

    def test_omits_empty_tag_values(self):
        line = influx.format_line_protocol(
            measurement='m',
            tags={'a': 'x', 'empty': '', 'none_like': 0},
            fields={'count': 1.0},
            timestamp=self.ts,
        )
        # Empty string and 0 are falsy — both skipped.
        self.assertNotIn('empty=', line)
        self.assertNotIn('none_like=', line)
        self.assertIn('a=x', line)

    def test_int_field_gets_i_suffix(self):
        line = influx.format_line_protocol(
            measurement='m',
            tags={'a': 'x'},
            fields={'n': 42},
            timestamp=self.ts,
        )
        self.assertIn('n=42i', line)

    def test_float_field_no_suffix(self):
        line = influx.format_line_protocol(
            measurement='m',
            tags={'a': 'x'},
            fields={'n': 1.5},
            timestamp=self.ts,
        )
        self.assertIn('n=1.5', line)
        self.assertNotIn('n=1.5i', line)

    def test_string_field_quoted_and_escaped(self):
        line = influx.format_line_protocol(
            measurement='m',
            tags={'a': 'x'},
            fields={'s': 'he said "hi"'},
            timestamp=self.ts,
        )
        self.assertIn('s="he said \\"hi\\""', line)

    def test_tag_value_special_chars_escaped(self):
        line = influx.format_line_protocol(
            measurement='m',
            tags={'tool': 'a b,c=d'},
            fields={'count': 1.0},
            timestamp=self.ts,
        )
        self.assertIn('tool=a\\ b\\,c\\=d', line)


class WriteToInfluxdbTests(unittest.TestCase):
    def setUp(self):
        self.env_patch = mock.patch.dict(
            'os.environ',
            {
                'INFLUX_URL': 'https://influx.example/',
                'INFLUX_DB': 'galaxy',
                'INFLUX_TOKEN': 'tok123',
            },
        )
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

    def _fake_response(self, status=204):
        resp = mock.MagicMock()
        resp.status = status
        resp.__enter__.return_value = resp
        resp.__exit__.return_value = False
        return resp

    @mock.patch('common.influx.urllib.request.urlopen')
    def test_posts_lines_joined_with_newlines(self, mock_open):
        mock_open.return_value = self._fake_response()
        influx.write_to_influxdb(['line1', 'line2'])
        request = mock_open.call_args[0][0]
        self.assertEqual(request.data, b'line1\nline2')
        self.assertEqual(request.get_method(), 'POST')
        self.assertEqual(
            request.headers['Authorization'], 'Token tok123')
        self.assertIn('db=galaxy', request.full_url)
        self.assertIn('precision=s', request.full_url)

    @mock.patch('common.influx.urllib.request.urlopen')
    def test_exits_on_url_error(self, mock_open):
        mock_open.side_effect = urllib.error.URLError('boom')
        with self.assertRaises(SystemExit):
            influx.write_to_influxdb(['line1'])


if __name__ == '__main__':
    unittest.main()
