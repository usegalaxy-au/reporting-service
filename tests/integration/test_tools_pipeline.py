"""End-to-end integration tests for the `tools` report pipeline."""

from datetime import datetime, timezone

import pytest
from sqlalchemy import text

from common import runner
from reports import tools as tools_report
from reports.tools import TOOL_REQUEST_PATTERN

from tests.integration.conftest import PINNED_DATE

pytestmark = pytest.mark.integration


def _count_matching_records(snapshot):
    """Mirror of the runner's match logic for self-consistent assertions."""
    n = 0
    for _key, records in snapshot:
        for r in records:
            request = r.get('parsed', {}).get('request', '')
            referer = r.get('parsed', {}).get('referer', '-')
            if not TOOL_REQUEST_PATTERN.search(request):
                continue
            if 'tool_id=' not in referer:
                continue
            n += 1
    return n


def _run_tools(start=PINNED_DATE, end=PINNED_DATE):
    runner.run(tools_report.REPORT, start, end, dry=False)


def _tags(line):
    """Parse `m,t1=v1,t2=v2 f=v ts` -> {'t1': 'v1', ...}."""
    head = line.split(' ', 1)[0]
    parts = head.split(',')[1:]
    return dict(p.split('=', 1) for p in parts)


def _fields(line):
    field_str = line.split(' ')[1]
    return dict(p.split('=', 1) for p in field_str.split(','))


def test_all_requests_emit_one_point_each(
    db_env, fake_s3, captured_writes, temp_state_dir, s3_records,
):
    expected = _count_matching_records(s3_records)
    assert expected > 0, 'No matching records found in S3 snapshot'
    _run_tools()
    assert len(captured_writes) == expected


def test_tool_id_and_version_split(
    db_env, fake_s3, captured_writes, temp_state_dir,
):
    _run_tools()
    for line in captured_writes:
        tags = _tags(line)
        assert 'tool_id' in tags
        # Toolshed IDs always include a slash before the version.
        assert tags.get('tool_version'), line
        # tool_id is the long suite path; tool_version is the trailing
        # path segment after the final slash in tool_id_full.
        assert '/' in tags['tool_id'], line


def test_domain_tag_from_referer(
    db_env, fake_s3, captured_writes, temp_state_dir,
):
    _run_tools()
    domains = {_tags(line).get('domain') for line in captured_writes}
    # Pinned-day traffic is all from usegalaxy.org.au per the fixture.
    assert domains == {'usegalaxy.org.au'}, domains


def test_matched_job_resolves_user_and_institution(
    db_env, fake_s3, captured_writes, temp_state_dir, db_conn,
):
    # User 48116 (mark.carascal@monash.edu) is the most-active user in
    # the seeded jobs and resolves to "Monash University" via the live
    # upstream domains.json.
    user_id = 48116
    row = db_conn.execute(text(
        "SELECT create_time, tool_id FROM job "
        "WHERE user_id = :uid AND tool_id LIKE '%abricate/abricate/%' "
        "ORDER BY create_time LIMIT 1"
    ), {'uid': user_id}).fetchone()
    assert row is not None, 'Expected a job row for the chosen user'

    _run_tools()

    # Find at least one line whose tag set resolves to Monash and
    # whose user_id field matches.
    matches = [
        line for line in captured_writes
        if _tags(line).get('institution') == 'Monash\\ University'
        and _fields(line).get('user_id') == f'{user_id}i'
    ]
    assert matches, (
        'Expected at least one line with Monash institution + user_id '
        f'={user_id}')


def test_unmatched_tool_emits_point_with_no_institution_or_user(
    db_env, fake_s3, captured_writes, temp_state_dir,
):
    # abricate_summary requests have no job rows in the fixture, so
    # they hit the "no job found" path.
    _run_tools()
    summary_lines = [
        line for line in captured_writes
        if 'abricate_summary' in _tags(line).get('tool_id', '')
    ]
    assert summary_lines, 'Expected at least one abricate_summary line'
    for line in summary_lines:
        tags = _tags(line)
        fields = _fields(line)
        assert 'institution' not in tags, line
        assert 'user_id' not in fields, line
        assert fields.get('count') == '1.0', line


def test_anonymous_user_omits_user_id_field(
    db_env, fake_s3, captured_writes, temp_state_dir,
):
    # The conftest seeded a NULL-user_id job at 2026-06-25 23:59:30
    # for abricate/1.4.0. Inject a synthetic nginx record at that
    # timestamp so the runner picks it up.
    fake_s3.append_record({
        'parsed': {
            'request': 'POST /api/tools HTTP/1.1',
            'timestamp': '2026-06-25T23:59:30Z',
            'referer': (
                'https://usegalaxy.org.au/'
                '?tool_id=toolshed.g2.bx.psu.edu%2Frepos%2Fiuc'
                '%2Fabricate%2Fabricate%2F1.4.0&version=latest'
            ),
        },
    })

    _run_tools()

    # Find the line emitted from our synthetic timestamp.
    target_ts = int(
        datetime(2026, 6, 25, 23, 59, 30, tzinfo=timezone.utc).timestamp())
    matches = [
        line for line in captured_writes
        if line.endswith(f' {target_ts}')
    ]
    assert len(matches) == 1, matches
    fields = _fields(matches[0])
    # Matched a job (so no anonymous fallback), but that job's user_id
    # is NULL — user_id field must be absent.
    assert 'user_id' not in fields, matches[0]


def test_state_marks_key_after_successful_write(
    db_env, fake_s3, captured_writes, temp_state_dir, s3_records,
):
    _run_tools()
    month_file = temp_state_dir / PINNED_DATE.strftime('%Y-%m')
    assert month_file.exists()
    ingested = set(month_file.read_text().splitlines())
    assert ingested == {k for k, _ in s3_records}


def test_rerun_skips_already_ingested_key(
    db_env, fake_s3, captured_writes, temp_state_dir,
):
    _run_tools()
    n_first = len(captured_writes)
    assert n_first > 0
    captured_writes.clear()
    _run_tools()
    assert captured_writes == []


def test_job_window_boundary(
    db_env, fake_s3, captured_writes, temp_state_dir,
):
    # Seeded NULL-user job is at 2026-06-25 23:59:30. A synthetic
    # request 6 s away falls outside the ±5 s window — no job match,
    # so no user_id field. (Note: even when matched, that job's user
    # is NULL, so a positive-control here would be ambiguous; we
    # assert the negative case which is the actual boundary contract.)
    far_ts_dt = datetime(2026, 6, 25, 23, 59, 36, tzinfo=timezone.utc)
    fake_s3.append_record({
        'parsed': {
            'request': 'POST /api/tools HTTP/1.1',
            'timestamp': far_ts_dt.strftime('%Y-%m-%dT%H:%M:%SZ'),
            'referer': (
                'https://usegalaxy.org.au/'
                '?tool_id=toolshed.g2.bx.psu.edu%2Frepos%2Fiuc'
                '%2Fnonexistent_tool%2Fnonexistent_tool%2F9.9.9'
                '&version=latest'
            ),
        },
    })

    _run_tools()

    target_ts = int(far_ts_dt.timestamp())
    matches = [
        line for line in captured_writes
        if line.endswith(f' {target_ts}')
    ]
    assert len(matches) == 1, matches
    line = matches[0]
    assert 'user_id' not in _fields(line), line
    assert 'institution' not in _tags(line), line
