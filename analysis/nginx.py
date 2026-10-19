"""Parser for raw nginx ``combined`` access log lines.

The pattern is deliberately RE2-compatible so the very same expression
can be used by DuckDB (``regexp_extract``) and by Python's ``re``.
"""

import re
from datetime import datetime, timezone

LINE_FIELDS = ('ip', 'ts', 'request', 'status', 'bytes', 'referer', 'ua')
LINE_PATTERN = (
    r'^(\S+) \S+ \S+ \[([^\]]+)\] "([^"]*)" (\d{3}|-) (\d+|-) '
    r'"([^"]*)" "([^"]*)"$'
)
REQUEST_FIELDS = ('method', 'target', 'protocol')
REQUEST_PATTERN = r'^([A-Za-z]+) (\S+)(?: (HTTP/[0-9.]+))?$'
TIMESTAMP_FORMAT = '%d/%b/%Y:%H:%M:%S %z'

LINE_RE = re.compile(LINE_PATTERN)
REQUEST_RE = re.compile(REQUEST_PATTERN)


def parse_line(line: str) -> dict | None:
    """Parse one combined-format line. Returns None if it doesn't match."""
    match = LINE_RE.match(line.rstrip('\n'))
    if not match:
        return None
    record = dict(zip(LINE_FIELDS, match.groups()))
    try:
        record['ts'] = datetime.strptime(
            record['ts'], TIMESTAMP_FORMAT).astimezone(timezone.utc)
    except ValueError:
        return None

    request = REQUEST_RE.match(record['request'])
    parts = request.groups() if request else (None, None, None)
    record.update(zip(REQUEST_FIELDS, parts))

    target = record['target'] or ''
    record['path'], _, record['query'] = target.partition('?')
    record['status'] = (
        None if record['status'] == '-' else int(record['status']))
    record['bytes'] = None if record['bytes'] == '-' else int(record['bytes'])
    record['referer'] = None if record['referer'] in ('', '-') else (
        record['referer'])
    record['ua'] = None if record['ua'] in ('', '-') else record['ua']
    return record
