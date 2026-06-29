"""Tool-run report.

Parses POST /api/tools (and /api/tools/fetch) submissions out of nginx
logs, recovers the tool_id from the referer, joins each submission to
the closest matching job row, resolves the submitter's institution, and
emits an InfluxDB data point per submission.
"""

import logging
import re
import urllib.parse
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from common.domains import lookup_institution
from common.influx import format_line_protocol
from common.runner import Report

logger = logging.getLogger(__name__)

DATETIME_FORMAT = '%Y-%m-%dT%H:%M:%SZ'
JOB_MATCH_WINDOW_SECONDS = 5

TOOL_REQUEST_PATTERN = re.compile(
    r'POST /api/tools(?:/fetch)?[\s?]'
)
REFERER_TOOL_ID_PATTERN = re.compile(
    r'[?&]tool_id=([^&"\s]+)'
)
DOMAIN_PATTERN = re.compile(
    r'https?://([^/"\s]+)'
)

JOB_QUERY = text("""
    SELECT j.id, j.tool_id, j.create_time, j.user_id, u.email
    FROM job j
    LEFT JOIN galaxy_user u ON u.id = j.user_id
    WHERE j.create_time BETWEEN :ts_start AND :ts_end
      AND j.tool_id = :tool_id
    ORDER BY ABS(EXTRACT(EPOCH FROM (j.create_time - :ts)))
    LIMIT 1
""")


def parse_record(record: dict) -> dict | None:
    """Extract tool run data from a JSON nginx log record."""
    parsed = record.get('parsed', {})
    request = parsed.get('request', '')

    if not TOOL_REQUEST_PATTERN.search(request):
        return None

    timestamp_str = parsed.get('timestamp', '')
    try:
        dt = datetime.strptime(timestamp_str, DATETIME_FORMAT).replace(
            tzinfo=timezone.utc)
    except ValueError:
        logger.warning("Unparseable timestamp in record: %s", timestamp_str)
        return None

    referer = parsed.get('referer', '-')
    domain_match = DOMAIN_PATTERN.search(referer)
    domain = domain_match.group(1) if domain_match else 'unknown'

    tool_id_match = REFERER_TOOL_ID_PATTERN.search(referer)
    if not tool_id_match:
        return None
    tool_id_full = urllib.parse.unquote(tool_id_match.group(1))
    tool_id, _, tool_version = tool_id_full.rpartition('/')
    if not tool_id:
        # Local tool with no version suffix
        tool_id = tool_id_full
        tool_version = ''

    return {
        'tool_id': tool_id,
        'tool_version': tool_version,
        'tool_id_full': tool_id_full,
        'datetime': dt,
        'domain': domain,
    }


def _get_job_for_record(conn, parsed: dict, domain_map: dict) -> dict | None:
    """Find the closest job row matching a parsed tool-run log record.

    A single nginx POST /api/tools request may produce many job rows (one
    per dataset in a collection submission); we only need one to recover
    the submitting user.
    """
    dt = parsed['datetime']
    result = conn.execute(
        JOB_QUERY,
        {
            'ts': dt,
            'ts_start': dt - timedelta(seconds=JOB_MATCH_WINDOW_SECONDS),
            'ts_end': dt + timedelta(seconds=JOB_MATCH_WINDOW_SECONDS),
            'tool_id': parsed['tool_id_full'],
        },
    ).fetchone()

    if not result:
        return None

    job_id, tool_id, create_time, user_id, email = result
    return {
        'job_id': job_id,
        'tool_id': tool_id,
        'create_time': create_time,
        'user_id': user_id,
        'email': email or '',
        'institution': lookup_institution(email or '', domain_map),
    }


def build_points(conn, parsed, domain_map, measurement) -> list[str]:
    job = _get_job_for_record(conn, parsed, domain_map)
    fields = {'count': 1.0}
    if job and job['user_id'] is not None:
        fields['user_id'] = job['user_id']
    return [format_line_protocol(
        measurement=measurement,
        tags={
            'domain': parsed['domain'],
            'tool_id': parsed['tool_id'],
            'tool_version': parsed['tool_version'],
            'institution': job['institution'] if job else '',
        },
        fields=fields,
        timestamp=parsed['datetime'],
    )]


REPORT = Report(
    name='tools',
    s3_prefix_env='S3_PREFIX_TOOL_RUNS',
    measurement='tool_runs',
    parse_record=parse_record,
    build_points=build_points,
)
