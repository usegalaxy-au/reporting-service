"""Collect tool run data from S3 nginx logs.

Downloads pre-formatted JSON nginx logs from S3 for a given date range,
and for each tool execution request (POST /api/tools/.../build):

- Extracts the tool ID from the request path
- Resolves the requesting site domain from the referer header
- Sends the data point to InfluxDB via the HTTP write API

Usage:
    tool_runs.py [--start YYYY-MM-DD] [--end YYYY-MM-DD]

    Both arguments are optional and inclusive. --start defaults to
    7 days ago and --end defaults to today, so with no arguments the
    script re-lists the last week of S3 objects. State files dedup the
    keys that have already been ingested, so the redundant LIST calls
    are cheap; the look-back exists to pick up Vector batches that flush
    to S3 hours or days after the events they contain.
"""

import argparse
import json
import logging
import os
import re
import ssl
import sys
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine, text

sys.path.insert(0, str(Path(__file__).parent.parent))
from s3 import S3Storage  # noqa: E402

load_dotenv(Path(__file__).parent.parent / '.env')

s3 = S3Storage(prefix=os.environ['S3_PREFIX_TOOL_RUNS'])


LOG_FORMAT = '%(asctime)s %(levelname)s: %(message)s'
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
logger = logging.getLogger(__name__)


def _log_uncaught(exc_type, exc_value, exc_tb):
    logger.critical(
        "Uncaught exception",
        exc_info=(exc_type, exc_value, exc_tb))


sys.excepthook = _log_uncaught

GALAXY_DATABASE_URL = os.environ['GALAXY_DATABASE_URL']
INFLUX_URL = os.environ['INFLUX_URL']
INFLUX_DB = os.environ['INFLUX_DB']
INFLUX_TOKEN = os.environ['INFLUX_TOKEN']
MEASUREMENT_NAME = 'tool_runs'
DATETIME_FORMAT = '%Y-%m-%dT%H:%M:%SZ'
STATE_DIR = Path(__file__).parent / 'state'
LOOKBACK_DAYS = 7

TOOL_REQUEST_PATTERN = re.compile(
    r'POST /api/tools(?:/fetch)?[\s?]'
)
REFERER_TOOL_ID_PATTERN = re.compile(
    r'[?&]tool_id=([^&"\s]+)'
)
DOMAIN_PATTERN = re.compile(
    r'https?://([^/"\s]+)'
)

DOMAINS_URL = (
    'https://raw.githubusercontent.com/usegalaxy-au/galaxy-media-site/'
    'refs/heads/dev/webapp/utils/data/domains.json'
)
DOMAINS_CACHE_FILE = STATE_DIR / 'domains.json'
DOMAINS_CACHE_TTL = timedelta(days=7)
JOB_MATCH_WINDOW_SECONDS = 5

JOB_QUERY = text("""
    SELECT j.id, j.tool_id, j.create_time, j.user_id, u.email
    FROM job j
    LEFT JOIN galaxy_user u ON u.id = j.user_id
    WHERE j.create_time BETWEEN :ts_start AND :ts_end
      AND j.tool_id = :tool_id
    ORDER BY ABS(EXTRACT(EPOCH FROM (j.create_time - :ts)))
    LIMIT 1
""")


def parse_log_record(record: dict) -> dict | None:
    """Extract tool run data from a JSON log record.

    Matches POST /api/tools (and /api/tools/fetch) submissions and recovers
    the tool_id from the referer's tool_id query parameter, since the
    submission endpoint URL itself does not carry the tool_id.

    Returns a dict with 'tool_id', 'tool_version', 'tool_id_full',
    'datetime', and 'domain' keys, or None if the record does not match
    a tool submission or the tool_id can't be recovered.
    """
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


def load_domain_map() -> dict:
    """Load email-domain -> institution map, refreshing weekly from GitHub."""
    needs_refresh = True
    if DOMAINS_CACHE_FILE.exists():
        mtime = datetime.fromtimestamp(DOMAINS_CACHE_FILE.stat().st_mtime)
        if datetime.now() - mtime < DOMAINS_CACHE_TTL:
            needs_refresh = False

    if needs_refresh:
        try:
            with urllib.request.urlopen(DOMAINS_URL) as resp:
                data = resp.read()
            json.loads(data)  # validate
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            DOMAINS_CACHE_FILE.write_bytes(data)
            logger.info("Refreshed domains.json cache")
        except (urllib.error.URLError, json.JSONDecodeError) as e:
            if DOMAINS_CACHE_FILE.exists():
                logger.warning(
                    "Failed to refresh domains.json, using stale cache: %s", e)
            else:
                logger.error("Failed to fetch domains.json: %s", e)
                return {}

    with DOMAINS_CACHE_FILE.open() as f:
        return json.load(f)


def lookup_institution(email: str, domain_map: dict) -> str:
    """Resolve an institution name from an email address using domain_map.

    domain_map keys are either '@full.domain' (exact) or '*.parent.tld'
    (suffix wildcard). Exact matches take precedence over wildcards.
    """
    if not email or '@' not in email:
        return ''
    domain = email.rsplit('@', 1)[1].lower()

    exact = domain_map.get('@' + domain)
    if exact:
        return exact

    best_match = ''
    best_len = 0
    for pattern, name in domain_map.items():
        if pattern.startswith('*.'):
            suffix = pattern[1:]
            if domain.endswith(suffix) and len(suffix) > best_len:
                best_match = name
                best_len = len(suffix)
    return best_match


def get_job_for_log_record(
    conn,
    parsed: dict,
    domain_map: dict,
    window_seconds: int = JOB_MATCH_WINDOW_SECONDS,
) -> dict | None:
    """Find the closest job row matching a parsed nginx tool-run log record.

    A single nginx POST /api/tools request may produce many job rows (one
    per dataset in a collection submission); we only need one to recover
    the submitting user. The closest match by create_time within a
    +/- window_seconds window is returned.

    Returns a dict with job metadata plus 'email' and 'institution', or
    None if no job was created within the window.
    """
    dt = parsed['datetime']
    result = conn.execute(
        JOB_QUERY,
        {
            'ts': dt,
            'ts_start': dt - timedelta(seconds=window_seconds),
            'ts_end': dt + timedelta(seconds=window_seconds),
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


def escape_tag_value(value: str) -> str:
    """Escape special characters in an InfluxDB line protocol tag value."""
    return (
        value
        .replace('\\', '\\\\')
        .replace(' ', '\\ ')
        .replace(',', '\\,')
        .replace('=', '\\=')
    )


def format_line_protocol(
    measurement: str,
    tags: dict,
    fields: dict,
    timestamp: datetime,
) -> str:
    """Format a data point as an InfluxDB line protocol string."""
    tag_str = ','.join(
        f"{k}={escape_tag_value(str(v))}"
        for k, v in tags.items()
        if v
    )
    field_parts = []
    for k, v in fields.items():
        if isinstance(v, float):
            field_parts.append(f"{k}={v}")
        elif isinstance(v, int):
            field_parts.append(f"{k}={v}i")
        else:
            field_parts.append(f'{k}="{str(v)}"')
    field_str = ','.join(field_parts)
    ts = int(timestamp.timestamp())
    return f"{measurement},{tag_str} {field_str} {ts}"


def load_ingested_keys(start_date: date, end_date: date) -> set[str]:
    """Load the set of S3 keys already ingested for the given date range.

    State files are named YYYY-MM and contain one ingested S3 key per line.
    A range spanning multiple months reads from all relevant state files.
    """
    months = set()
    current = start_date
    while current <= end_date:
        months.add(current.strftime('%Y-%m'))
        current += timedelta(days=1)

    ingested = set()
    for month in months:
        state_file = STATE_DIR / month
        if not state_file.exists():
            continue
        with state_file.open() as f:
            for line in f:
                line = line.strip()
                if line:
                    ingested.add(line)
    return ingested


def mark_ingested(key: str):
    """Append an S3 key to the state file for the month it belongs to."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    month = s3.date_from_key(key).strftime('%Y-%m')
    state_file = STATE_DIR / month
    with state_file.open('a') as f:
        f.write(key + '\n')


def write_to_influxdb(lines: list[str]):
    """Write line protocol data points to InfluxDB via the HTTP write API."""
    payload = '\n'.join(lines).encode('utf-8')
    url = f"{INFLUX_URL}/write?db={INFLUX_DB}&precision=s"
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            'Authorization': f'Token {INFLUX_TOKEN}',
            'Content-Type': 'application/octet-stream',
        },
        method='POST',
    )
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(req, context=ctx) as response:
            logger.info(
                "Wrote %d data points to InfluxDB (HTTP %d)",
                len(lines), response.status,
            )
    except urllib.error.URLError as e:
        logger.error("Failed to write to InfluxDB: %s", e)
        sys.exit(1)


def parse_args() -> tuple[date, date]:
    parser = argparse.ArgumentParser(
        description="Collect tool run data from S3 nginx logs.",
    )
    parser.add_argument(
        '--start',
        type=date.fromisoformat,
        default=None,
        help=(
            "Start date (YYYY-MM-DD, inclusive). Defaults to "
            f"{LOOKBACK_DAYS} days ago."
        ),
    )
    parser.add_argument(
        '--end',
        type=date.fromisoformat,
        default=None,
        help="End date (YYYY-MM-DD, inclusive). Defaults to today.",
    )
    args = parser.parse_args()
    today = date.today()
    start_date = args.start or today - timedelta(days=LOOKBACK_DAYS)
    end_date = args.end or today
    if start_date > end_date:
        parser.error(
            f"--start ({start_date}) is after --end ({end_date})")
    return start_date, end_date


def main():
    start_date, end_date = parse_args()
    ingested = load_ingested_keys(start_date, end_date)
    domain_map = load_domain_map()
    engine = create_engine(GALAXY_DATABASE_URL)

    run_records = 0
    run_tool_matches = 0
    run_job_matches = 0
    run_with_institution = 0

    with engine.connect() as conn:
        for key in s3.iter_keys(start_date, end_date):
            if key in ingested:
                logger.debug("Skipping already-ingested key: %s", key)
                continue

            data_points = []
            total_records = 0
            tool_matches = 0
            job_matches = 0
            with_institution = 0
            for record in s3.read_records(key):
                total_records += 1
                parsed = parse_log_record(record)
                if not parsed:
                    continue
                tool_matches += 1

                job = get_job_for_log_record(conn, parsed, domain_map)
                if job:
                    job_matches += 1
                    if job['institution']:
                        with_institution += 1

                data_points.append(format_line_protocol(
                    measurement=MEASUREMENT_NAME,
                    tags={
                        'domain': parsed['domain'],
                        'tool_id': parsed['tool_id'],
                        'tool_version': parsed['tool_version'],
                        'institution': job['institution'] if job else '',
                    },
                    fields={
                        'count': 1.0,
                        **(
                            {'user_id': job['user_id']}
                            if job and job['user_id'] is not None
                            else {}
                        ),
                    },
                    timestamp=parsed['datetime'],
                ))

            run_records += total_records
            run_tool_matches += tool_matches
            run_job_matches += job_matches
            run_with_institution += with_institution

            logger.info(
                "%s: %d/%d records matched tool pattern; "
                "%d/%d matched a DB job; %d/%d resolved an institution",
                key.split('/')[-1],
                tool_matches, total_records,
                job_matches, tool_matches,
                with_institution, job_matches,
            )
            if data_points:
                write_to_influxdb(data_points)
                mark_ingested(key)
            else:
                logger.warning(
                    "No tool run records found in %s — "
                    "key not marked ingested",
                    key,
                )

    logger.info(
        "Run summary: %d records, %d tool submissions, "
        "%d matched to DB jobs (%.1f%%), %d resolved institutions",
        run_records,
        run_tool_matches,
        run_job_matches,
        (100.0 * run_job_matches / run_tool_matches)
        if run_tool_matches else 0.0,
        run_with_institution,
    )


if __name__ == '__main__':
    main()
