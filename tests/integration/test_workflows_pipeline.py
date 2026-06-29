"""End-to-end integration tests for the `workflows` report pipeline."""

import codecs
from datetime import datetime, timezone

import pytest
from sqlalchemy import text

from common import runner
from reports import workflows as workflows_report
from reports.workflows import INVOCATION_PATTERN, decode_galaxy_id

from tests.integration.conftest import PINNED_DATE

pytestmark = pytest.mark.integration


# Workflow ids in workflows.tsv whose source_metadata embeds a TRS tag.
TRS_WORKFLOW_IDS = {135141, 135159, 135592}
# Workflow ids with empty source_metadata — canonical_id should fall
# back to workflow name in the emitted line protocol.
EMPTY_METADATA_WORKFLOW_IDS = {135590, 135591, 135593, 135597}


def _split_unescaped(s, sep, maxsplit=-1):
    """Split `s` on `sep`, ignoring backslash-escaped separators."""
    parts = []
    cur = []
    i = 0
    while i < len(s):
        c = s[i]
        if c == '\\' and i + 1 < len(s):
            cur.append(c)
            cur.append(s[i + 1])
            i += 2
        elif c == sep and (maxsplit < 0 or len(parts) < maxsplit):
            parts.append(''.join(cur))
            cur = []
            i += 1
        else:
            cur.append(c)
            i += 1
    parts.append(''.join(cur))
    return parts


def _tags(line):
    head = _split_unescaped(line, ' ', maxsplit=2)[0]
    tag_parts = _split_unescaped(head, ',')[1:]
    return dict(_split_unescaped(p, '=', maxsplit=1) for p in tag_parts)


def _fields(line):
    parts = _split_unescaped(line, ' ', maxsplit=2)
    field_str = parts[1]
    return dict(
        _split_unescaped(p, '=', maxsplit=1)
        for p in _split_unescaped(field_str, ',')
    )


def _decoded_invocation_ids(snapshot):
    """Yield the decoded stored_workflow.id for every invocation."""
    for _key, records in snapshot:
        for r in records:
            request = r.get('parsed', {}).get('request', '')
            m = INVOCATION_PATTERN.search(request)
            if not m:
                continue
            try:
                yield decode_galaxy_id(m.group(1))
            except (ValueError, TypeError):
                continue


def _encode_galaxy_id(decoded_id: int) -> str:
    """Inverse of decode_galaxy_id, using the live cipher."""
    cipher = workflows_report.get_id_cipher()
    s = str(decoded_id).encode('utf-8')
    pad = (-len(s)) % 8
    s = b'!' * pad + s
    return codecs.encode(cipher.encrypt(s), 'hex').decode()


def _run_workflows(start=PINNED_DATE, end=PINNED_DATE):
    runner.run(workflows_report.REPORT, start, end, dry=False)


def _seeded_workflow_ids(db_conn):
    rows = db_conn.execute(text("SELECT id FROM stored_workflow")).fetchall()
    return {r[0] for r in rows}


def test_all_invocations_emit_one_point_each_for_known_workflows(
    db_env, fake_s3_workflows, captured_writes, temp_state_dir_workflows,
    s3_workflow_records, db_conn,
):
    seeded = _seeded_workflow_ids(db_conn)
    expected = sum(
        1 for sw_id in _decoded_invocation_ids(s3_workflow_records)
        if sw_id in seeded
    )
    assert expected > 0, 'No matching workflow invocations in S3 snapshot'

    _run_workflows()

    assert len(captured_writes) == expected


def test_workflow_name_tag_set_from_db(
    db_env, fake_s3_workflows, captured_writes, temp_state_dir_workflows,
    db_conn,
):
    rows = db_conn.execute(
        text("SELECT name FROM stored_workflow")
    ).fetchall()
    known_names = {r[0] for r in rows}

    _run_workflows()
    assert captured_writes, 'Expected at least one captured line'

    for line in captured_writes:
        tags = _tags(line)
        assert 'workflow_name' in tags, line
        # Reverse the line-protocol tag escaping for the comparison.
        unescaped = (
            tags['workflow_name']
            .replace('\\,', ',')
            .replace('\\=', '=')
            .replace('\\ ', ' ')
            .replace('\\\\', '\\')
        )
        assert unescaped in known_names, line


def test_canonical_id_from_trs_metadata(
    db_env, fake_s3_workflows, captured_writes, temp_state_dir_workflows,
    db_conn,
):
    # Map the TRS workflow.id values to their stored_workflow names so
    # we can locate the captured lines by workflow_name tag.
    rows = db_conn.execute(text(
        "SELECT sw.name FROM stored_workflow sw "
        "WHERE sw.latest_workflow_id = ANY(:ids)"
    ), {'ids': list(TRS_WORKFLOW_IDS)}).fetchall()
    trs_names = {r[0] for r in rows}
    assert trs_names, 'Expected stored_workflow rows for TRS workflow ids'

    _run_workflows()

    matched = [
        line for line in captured_writes
        if _tags(line).get('workflow_name', '')
        .replace('\\,', ',').replace('\\=', '=')
        .replace('\\ ', ' ').replace('\\\\', '\\') in trs_names
    ]
    assert matched, (
        'Expected at least one captured line for a TRS workflow; '
        f'TRS names: {trs_names}'
    )

    for line in matched:
        tags = _tags(line)
        fields = _fields(line)
        # canonical_id is "<trs_server>:<trs_tool_id>", and trs_server
        # is "workflowhub" for every TRS row in workflows.tsv.
        assert tags.get('canonical_id', '').startswith('workflowhub:'), line
        assert tags.get('trs_server') == 'workflowhub', line
        # trs_version_id is a string field — quoted, non-empty.
        assert fields.get('trs_version_id', '""') != '""', line


def test_canonical_id_falls_back_to_name_when_metadata_empty(
    db_env, fake_s3_workflows, captured_writes, temp_state_dir_workflows,
    db_conn,
):
    rows = db_conn.execute(text(
        "SELECT sw.name FROM stored_workflow sw "
        "WHERE sw.latest_workflow_id = ANY(:ids)"
    ), {'ids': list(EMPTY_METADATA_WORKFLOW_IDS)}).fetchall()
    empty_names = {r[0] for r in rows}
    assert empty_names

    _run_workflows()

    matched = [
        line for line in captured_writes
        if _tags(line).get('workflow_name', '')
        .replace('\\,', ',').replace('\\=', '=')
        .replace('\\ ', ' ').replace('\\\\', '\\') in empty_names
    ]
    if not matched:
        pytest.skip('No invocations for empty-metadata workflows in snapshot')

    for line in matched:
        tags = _tags(line)
        # When source_metadata is empty, resolve_canonical_id returns
        # ('', '', '') and build_points uses `canonical_id or name`.
        assert tags.get('canonical_id') == tags.get('workflow_name'), line
        # trs_server tag is empty → omitted by format_line_protocol.
        assert 'trs_server' not in tags, line


def test_domain_tag_from_referer(
    db_env, fake_s3_workflows, captured_writes, temp_state_dir_workflows,
):
    _run_workflows()
    assert captured_writes
    domains = {_tags(line).get('domain') for line in captured_writes}
    # Workflow invocations on the pinned day all come from usegalaxy.org.au.
    assert domains == {'usegalaxy.org.au'}, domains


def test_institution_tag_from_user_email(
    db_env, fake_s3_workflows, captured_writes, temp_state_dir_workflows,
):
    _run_workflows()
    # At least one line should resolve a non-empty institution via the
    # live upstream domains.json (e.g. daf.qld.gov.au or monash.edu).
    with_inst = [
        line for line in captured_writes
        if _tags(line).get('institution')
    ]
    assert with_inst, (
        'Expected at least one captured line with a non-empty institution '
        'tag (domains.json lookup may be down)'
    )


def test_unknown_encoded_id_emits_no_point(
    db_env, fake_s3_workflows, captured_writes, temp_state_dir_workflows,
):
    bogus_encoded = _encode_galaxy_id(987654321)
    ts_dt = datetime(2026, 6, 25, 23, 59, 0, tzinfo=timezone.utc)
    fake_s3_workflows.append_record({
        'parsed': {
            'request': (
                f'POST /api/workflows/{bogus_encoded}/invocations HTTP/1.1'
            ),
            'timestamp': ts_dt.strftime('%Y-%m-%dT%H:%M:%SZ'),
            'referer': 'https://usegalaxy.org.au/workflows/run',
        },
    })

    _run_workflows()

    suffix = f' {int(ts_dt.timestamp())}'
    bogus_lines = [
        line for line in captured_writes if line.endswith(suffix)
    ]
    assert bogus_lines == [], bogus_lines


def test_malformed_encoded_id_emits_no_point(
    db_env, fake_s3_workflows, captured_writes, temp_state_dir_workflows,
):
    # 8-byte cipher block of all zeros decrypts to garbage that fails
    # int(...) parsing — exercises the ValueError handler in build_points.
    ts_dt = datetime(2026, 6, 25, 23, 58, 0, tzinfo=timezone.utc)
    fake_s3_workflows.append_record({
        'parsed': {
            'request': (
                'POST /api/workflows/0000000000000000/invocations HTTP/1.1'
            ),
            'timestamp': ts_dt.strftime('%Y-%m-%dT%H:%M:%SZ'),
            'referer': 'https://usegalaxy.org.au/workflows/run',
        },
    })

    _run_workflows()

    suffix = f' {int(ts_dt.timestamp())}'
    malformed_lines = [
        line for line in captured_writes if line.endswith(suffix)
    ]
    assert malformed_lines == [], malformed_lines


def test_state_marks_key_after_successful_write(
    db_env, fake_s3_workflows, captured_writes, temp_state_dir_workflows,
    s3_workflow_records,
):
    _run_workflows()
    month_file = temp_state_dir_workflows / PINNED_DATE.strftime('%Y-%m')
    assert month_file.exists()
    ingested = set(month_file.read_text().splitlines())
    # Only keys that produced at least one data point get marked
    # (runner.run skips state-marking when data_points is empty).
    assert ingested.issubset({k for k, _ in s3_workflow_records})
    assert ingested  # at least one key was marked


def test_rerun_skips_already_ingested_key(
    db_env, fake_s3_workflows, captured_writes, temp_state_dir_workflows,
):
    _run_workflows()
    n_first = len(captured_writes)
    assert n_first > 0
    captured_writes.clear()
    _run_workflows()
    assert captured_writes == []
