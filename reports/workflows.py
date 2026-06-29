"""Workflow-invocation report.

Parses POST /api/workflows/<encoded_id>/invocations requests, decodes
the StoredWorkflow ID using Galaxy's IdEncodingHelper algorithm, looks
up workflow metadata, resolves a canonical identity (TRS tool ID if
available), and emits an InfluxDB data point per invocation.
"""

import codecs
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from Crypto.Cipher import Blowfish
from sqlalchemy import text

from common.domains import lookup_institution
from common.influx import format_line_protocol
from common.runner import Report

logger = logging.getLogger(__name__)

DATETIME_FORMAT = '%Y-%m-%dT%H:%M:%SZ'

INVOCATION_PATTERN = re.compile(
    r'POST /api/workflows/([a-f0-9]+)/invocations'
)
DOMAIN_PATTERN = re.compile(
    r'https?://([^/"\s]+)'
)

WORKFLOW_QUERY = text("""
    SELECT sw.id, sw.name, sw.user_id, w.uuid, w.source_metadata, u.email
    FROM stored_workflow sw
    JOIN workflow w ON w.id = sw.latest_workflow_id
    LEFT JOIN galaxy_user u ON u.id = sw.user_id
    WHERE sw.id = :id
""")


_id_cipher = None


def get_id_cipher():
    """Lazily build the Blowfish cipher used to decode Galaxy IDs.

    Built on first use so importing this module does not require
    GALAXY_ID_SECRET to be set (relevant for testing/inspection).
    """
    global _id_cipher
    if _id_cipher is None:
        _id_cipher = Blowfish.new(
            os.environ['GALAXY_ID_SECRET'].encode('utf-8'),
            mode=Blowfish.MODE_ECB,
        )
    return _id_cipher


def decode_galaxy_id(encoded_id: str, id_cipher=None) -> int:
    """Decode a Galaxy hex-encoded ID to an integer database ID.

    Replicates Galaxy's IdEncodingHelper.decode_id algorithm:
    hex decode -> Blowfish ECB decrypt -> strip padding -> int.
    """
    if id_cipher is None:
        id_cipher = get_id_cipher()
    raw = codecs.decode(encoded_id, 'hex')
    decrypted = id_cipher.decrypt(raw)
    return int(decrypted.decode('utf-8').lstrip('!'))


def resolve_canonical_id(source_metadata) -> tuple[str, str, str]:
    """Resolve canonical workflow identity from source_metadata.

    Returns (canonical_id, trs_server, trs_version_id).
    """
    if not source_metadata:
        return ('', '', '')

    if isinstance(source_metadata, memoryview):
        source_metadata = bytes(source_metadata).decode('utf-8')

    if isinstance(source_metadata, str):
        source_metadata = json.loads(source_metadata)

    trs_tool_id = source_metadata.get('trs_tool_id', '')
    trs_server = source_metadata.get('trs_server', '')
    trs_version_id = source_metadata.get('trs_version_id', '')

    if trs_tool_id:
        canonical_id = (
            f"{trs_server}:{trs_tool_id}" if trs_server
            else trs_tool_id
        )
        return (canonical_id, trs_server, trs_version_id)

    url = source_metadata.get('url', '')
    if url:
        return (url, '', '')

    return ('', '', '')


def parse_record(record: dict) -> dict | None:
    """Extract workflow invocation data from a JSON nginx log record."""
    parsed = record.get('parsed', {})
    request = parsed.get('request', '')

    inv_match = INVOCATION_PATTERN.search(request)
    if not inv_match:
        return None

    ts_str = parsed.get('timestamp', '')
    try:
        dt = datetime.strptime(ts_str, DATETIME_FORMAT).replace(
            tzinfo=timezone.utc)
    except ValueError:
        logger.warning("Unparseable timestamp in record: %s", ts_str)
        return None

    referer = parsed.get('referer', '-')
    domain_match = DOMAIN_PATTERN.search(referer)
    domain = domain_match.group(1) if domain_match else 'unknown'

    return {
        'encoded_id': inv_match.group(1),
        'datetime': dt,
        'domain': domain,
    }


def build_points(conn, parsed, domain_map, measurement) -> list[str]:
    try:
        workflow_id = decode_galaxy_id(parsed['encoded_id'])
    except (ValueError, TypeError) as e:
        logger.warning(
            "Failed to decode ID '%s': %s", parsed['encoded_id'], e)
        return []

    result = conn.execute(
        WORKFLOW_QUERY, {'id': workflow_id}).fetchone()
    if not result:
        logger.warning(
            "StoredWorkflow %d not found in database", workflow_id)
        return []

    _, name, user_id, _uuid, source_metadata, email = result
    canonical_id, trs_server, trs_version_id = (
        resolve_canonical_id(source_metadata)
    )
    institution = lookup_institution(email or '', domain_map)

    return [format_line_protocol(
        measurement=measurement,
        tags={
            'domain': parsed['domain'],
            'workflow_name': name,
            'canonical_id': canonical_id or name,
            'trs_server': trs_server,
            'institution': institution,
        },
        fields={
            'count': 1.0,
            'workflow_id': workflow_id,
            'user_id': user_id,
            'trs_version_id': trs_version_id,
        },
        timestamp=parsed['datetime'],
    )]


REPORT = Report(
    name='workflows',
    s3_prefix_env='S3_PREFIX_WORKFLOW_INVOCATIONS',
    measurement='workflow_invocation',
    state_dir=Path(__file__).parent.parent / 'workflows' / 'state',
    parse_record=parse_record,
    build_points=build_points,
)
