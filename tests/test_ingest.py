import shutil

import duckdb

from analysis.ingest import PARQUET_DIRNAME, ingest
from tests.test_nginx import SAMPLE


def _ingest(tmp_path):
    logs = tmp_path / 'logs'
    logs.mkdir()
    shutil.copy(SAMPLE, logs / 'sample.log')
    out = tmp_path / 'out'
    ingest(logs, out, memory_limit='256MB', threads=1)
    rows = duckdb.connect().execute(
        f"SELECT ip, path, api_kind, scanner_match FROM read_parquet("
        f"'{out / PARQUET_DIRNAME / '*.parquet'}') ORDER BY ALL"
    ).fetchall()
    return {(ip, path): (kind, match) for ip, path, kind, match in rows}


def test_unknown_api_route_is_labelled_but_not_a_scanner_match(tmp_path):
    rows = _ingest(tmp_path)
    # A client bug, not an attack: labelled bogus_api, never malicious.
    assert rows[('203.0.113.4', '/api/api/workflows/abc/download')] == (
        'bogus_api', None)


def test_scanner_and_malformed_requests_are_matched(tmp_path):
    rows = _ingest(tmp_path)
    assert rows[('203.0.113.3', '/wp-login.php')][1] == 'php'
    assert rows[('203.0.113.3', '/api/smtp/smtp%2ejson')] == (
        'bogus_api', 'encoded_dot_file')
    assert rows[('203.0.113.5', None)] == ('none', 'malformed')


def test_genuine_requests_are_not_matched(tmp_path):
    rows = _ingest(tmp_path)
    assert rows[('203.0.113.2', '/api/histories')] == ('legit_api', None)
    assert rows[('203.0.113.7', '/.well-known/passkey-endpoints')] == (
        'none', None)
