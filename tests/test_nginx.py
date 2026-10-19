from datetime import datetime, timezone
from pathlib import Path

from analysis.nginx import parse_line

SAMPLE = Path(__file__).parent / 'data' / 'nginx-combined-sample.txt'


def _lines():
    return SAMPLE.read_text().splitlines()


def test_parses_ui_poll_line():
    record = parse_line(_lines()[0])
    assert record['ip'] == '203.0.113.1'
    assert record['method'] == 'GET'
    assert record['path'] == '/history/current_history_json'
    assert record['query'] == 'since=2026-08-01T04:06:22'
    assert record['status'] == 200
    assert record['bytes'] == 35
    assert record['referer'] == 'https://usegalaxy.org.au/'
    assert record['ts'] == datetime(
        2026, 8, 3, 23, 0, 3, tzinfo=timezone.utc)


def test_dash_fields_become_none():
    record = parse_line(_lines()[2])
    assert record['referer'] is None
    assert record['ua'] is None


def test_malformed_request_has_no_method():
    record = parse_line(_lines()[6])
    assert record['method'] is None
    assert record['path'] == ''
    assert record['status'] == 400


def test_non_log_line_returns_none():
    assert parse_line(_lines()[-1]) is None


def test_every_sample_line_but_the_last_parses():
    parsed = [parse_line(line) for line in _lines()]
    assert parsed[-1] is None
    assert all(parsed[:-1])
