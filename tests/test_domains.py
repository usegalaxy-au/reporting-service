"""Tests for common.domains."""

import json
import os
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from common import domains


class LookupInstitutionTests(unittest.TestCase):
    DOMAIN_MAP = {
        '@uq.edu.au': 'University of Queensland',
        '*.edu.au': 'Australian University',
        '*.com': 'Generic Commercial',
    }

    def test_returns_empty_for_blank_email(self):
        self.assertEqual(domains.lookup_institution('', self.DOMAIN_MAP), '')

    def test_returns_empty_for_email_without_at(self):
        self.assertEqual(
            domains.lookup_institution('notanemail', self.DOMAIN_MAP), '')

    def test_exact_match_preferred_over_wildcard(self):
        self.assertEqual(
            domains.lookup_institution('jane@uq.edu.au', self.DOMAIN_MAP),
            'University of Queensland',
        )

    def test_wildcard_match_when_no_exact(self):
        self.assertEqual(
            domains.lookup_institution('joe@anu.edu.au', self.DOMAIN_MAP),
            'Australian University',
        )

    def test_longest_wildcard_wins(self):
        domain_map = {
            '*.edu.au': 'AU University',
            '*.au': 'Australia',
        }
        # *.edu.au (7 chars) is longer than *.au (3) — wins.
        self.assertEqual(
            domains.lookup_institution('a@x.edu.au', domain_map),
            'AU University',
        )

    def test_returns_empty_when_no_match(self):
        self.assertEqual(
            domains.lookup_institution('a@example.org', self.DOMAIN_MAP),
            '',
        )

    def test_domain_lowercased(self):
        self.assertEqual(
            domains.lookup_institution('jane@UQ.EDU.AU', self.DOMAIN_MAP),
            'University of Queensland',
        )


class LoadDomainMapTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cache_file = Path(self._tmp.name) / 'sub' / 'domains.json'

    def _fake_response(self, body: bytes):
        resp = mock.MagicMock()
        resp.read.return_value = body
        resp.__enter__.return_value = resp
        resp.__exit__.return_value = False
        return resp

    @mock.patch('common.domains.urllib.request.urlopen')
    def test_fetches_when_no_cache(self, mock_open):
        body = json.dumps({'@a.com': 'A'}).encode()
        mock_open.return_value = self._fake_response(body)

        result = domains.load_domain_map(self.cache_file)

        self.assertEqual(result, {'@a.com': 'A'})
        self.assertTrue(self.cache_file.exists())
        self.assertEqual(self.cache_file.read_bytes(), body)

    @mock.patch('common.domains.urllib.request.urlopen')
    def test_uses_fresh_cache_without_fetching(self, mock_open):
        self.cache_file.parent.mkdir(parents=True, exist_ok=True)
        self.cache_file.write_text(json.dumps({'@cached.com': 'C'}))

        result = domains.load_domain_map(self.cache_file)

        self.assertEqual(result, {'@cached.com': 'C'})
        mock_open.assert_not_called()

    @mock.patch('common.domains.urllib.request.urlopen')
    def test_refetches_when_cache_stale(self, mock_open):
        self.cache_file.parent.mkdir(parents=True, exist_ok=True)
        self.cache_file.write_text(json.dumps({'@old.com': 'O'}))
        stale = (
            datetime.now() - domains.DOMAINS_CACHE_TTL - timedelta(days=1)
        ).timestamp()
        os.utime(self.cache_file, (stale, stale))

        new_body = json.dumps({'@new.com': 'N'}).encode()
        mock_open.return_value = self._fake_response(new_body)

        result = domains.load_domain_map(self.cache_file)

        self.assertEqual(result, {'@new.com': 'N'})
        mock_open.assert_called_once()

    @mock.patch('common.domains.urllib.request.urlopen')
    def test_falls_back_to_stale_cache_on_fetch_error(self, mock_open):
        self.cache_file.parent.mkdir(parents=True, exist_ok=True)
        self.cache_file.write_text(json.dumps({'@stale.com': 'S'}))
        stale = (
            datetime.now() - domains.DOMAINS_CACHE_TTL - timedelta(days=1)
        ).timestamp()
        os.utime(self.cache_file, (stale, stale))

        mock_open.side_effect = urllib.error.URLError('no network')

        result = domains.load_domain_map(self.cache_file)

        self.assertEqual(result, {'@stale.com': 'S'})

    @mock.patch('common.domains.urllib.request.urlopen')
    def test_returns_empty_on_fetch_error_with_no_cache(self, mock_open):
        mock_open.side_effect = urllib.error.URLError('no network')

        result = domains.load_domain_map(self.cache_file)

        self.assertEqual(result, {})
        self.assertFalse(self.cache_file.exists())

    @mock.patch('common.domains.urllib.request.urlopen')
    def test_rejects_invalid_json_response(self, mock_open):
        mock_open.return_value = self._fake_response(b'not json')

        result = domains.load_domain_map(self.cache_file)

        # No cache existed and the fetch was unparseable -> empty map.
        self.assertEqual(result, {})
        self.assertFalse(self.cache_file.exists())


if __name__ == '__main__':
    unittest.main()
