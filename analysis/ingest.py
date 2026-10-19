"""Ingest raw nginx logs into Parquet (one file per source log) via DuckDB.

Each Parquet row holds the raw ``line`` plus parsed and derived columns.
Lines which don't match the combined format are kept with ``ip`` NULL so
the unparsed count is auditable straight from the Parquet files.
"""

import logging
from pathlib import Path

import duckdb

from analysis import classify, nginx

logger = logging.getLogger(__name__)

LOG_GLOBS = ('*.log', '*.log.gz')
PARQUET_DIRNAME = 'parquet'
# Unlikely-to-occur delimiter so each line lands in a single CSV column.
LINE_DELIMITER = '\x1f'
DEFAULT_MEMORY_LIMIT = '6GB'
SUBNET24_RE = r'^(\d+\.\d+\.\d+)\.\d+$'
SUBNET16_RE = r'^(\d+\.\d+)\.\d+\.\d+$'


def _fields_sql(table: str = 'f') -> str:
    return ', '.join(f"{table}.{name}" for name in nginx.LINE_FIELDS)


def build_select_sql(self_host: str) -> str:
    """Return the SELECT turning a ``?`` log path into the full table."""
    q = classify._sql_quote
    return f"""
    WITH raw AS (
        SELECT line,
               regexp_extract(line, {q(nginx.LINE_PATTERN)},
                   {list(nginx.LINE_FIELDS)}) AS f
        FROM read_csv(?, columns={{'line': 'VARCHAR'}},
                      delim={q(LINE_DELIMITER)}, quote='', escape='',
                      header=false, strict_mode=false, parallel=true)
    ), parsed AS (
        SELECT line,
            NULLIF(f.ip, '') AS ip,
            try_strptime(f.ts, {q(nginx.TIMESTAMP_FORMAT)})::TIMESTAMP AS ts,
            f.request AS request,
            TRY_CAST(NULLIF(f.status, '-') AS INTEGER) AS status,
            TRY_CAST(NULLIF(f.bytes, '-') AS BIGINT) AS bytes,
            NULLIF(NULLIF(f.referer, '-'), '') AS referer,
            NULLIF(NULLIF(f.ua, '-'), '') AS ua,
            regexp_extract(f.request, {q(nginx.REQUEST_PATTERN)},
                {list(nginx.REQUEST_FIELDS)}) AS r
        FROM raw
    ), split AS (
        SELECT line, ip, ts, request, status, bytes, referer, ua,
            NULLIF(r.method, '') AS method,
            NULLIF(r.target, '') AS target,
            NULLIF(r.protocol, '') AS protocol,
            split_part(NULLIF(r.target, ''), '?', 1) AS path
        FROM parsed
    )
    SELECT line, ip, ts, method, target, protocol, path,
        CASE WHEN instr(target, '?') > 0
             THEN substr(target, instr(target, '?') + 1) END AS query,
        status, bytes, referer, ua, request,
        ua IS NULL AS ua_empty,
        CASE WHEN regexp_matches(ip, {q(SUBNET24_RE)})
             THEN regexp_extract(ip, {q(SUBNET24_RE)}, 1) || '.0/24'
             ELSE ip END AS subnet24,
        CASE WHEN regexp_matches(ip, {q(SUBNET16_RE)})
             THEN regexp_extract(ip, {q(SUBNET16_RE)}, 1) || '.0.0/16'
             ELSE ip END AS subnet16,
        coalesce(regexp_matches(referer, ?), false) AS self_referer,
        coalesce(regexp_matches(ua, {q(classify.CRAWLER_UA_PATTERN)}),
                 false) AS is_crawler,
        coalesce(regexp_matches(ua, {q(classify.MONITOR_UA_PATTERN)}),
                 false) AS is_monitor,
        coalesce(regexp_matches(ua, {q(classify.TOOL_UA_PATTERNS)}),
                 false) AS ua_tool,
        coalesce(regexp_matches(ua, {q(classify.GENERIC_LIB_UA_PATTERNS)}),
                 false) AS ua_generic_lib,
        path IN ({classify.sql_in(classify.UI_POLL_PATHS)}) AS is_ui_poll,
        {_api_scanner_sql()}
    FROM split
    """


def _api_scanner_sql() -> str:
    """Final two columns: api_kind and scanner_match.

    api_kind ignores the 4xx-only patterns: a legit route stays legit
    whatever it returns.
    """
    q = classify._sql_quote
    scan = classify.scanner_case_sql('target')
    scan_4xx = classify.scanner_case_sql('target', 'status')
    return f"""
        CASE
            WHEN path IS NULL
                 OR NOT regexp_matches(path, {q(classify.API_PREFIX_RE)})
                THEN 'none'
            WHEN regexp_matches(path, {q(classify.GALAXY_API_RE)})
                 AND ({scan}) IS NULL THEN 'legit_api'
            ELSE {q(classify.BOGUS_API_NAME)}
        END AS api_kind,
        coalesce(({scan_4xx}),
            CASE WHEN method IS NULL THEN {q(classify.MALFORMED_NAME)}
                 WHEN regexp_matches(path, {q(classify.API_PREFIX_RE)})
                      AND NOT regexp_matches(path, {q(classify.GALAXY_API_RE)})
                 THEN {q(classify.BOGUS_API_NAME)} END) AS scanner_match
    """


def connect(
    out_dir: Path,
    memory_limit: str = DEFAULT_MEMORY_LIMIT,
    threads: int | None = None,
) -> duckdb.DuckDBPyConnection:
    """DuckDB connection configured for (possibly small) analysis hosts."""
    con = duckdb.connect()
    con.execute(f"SET memory_limit='{memory_limit}'")
    if threads:
        con.execute(f'SET threads={int(threads)}')
    # Lets DuckDB stream/spill instead of holding results in order.
    con.execute('SET preserve_insertion_order=false')
    con.execute("SET TimeZone='UTC'")
    con.execute('SET enable_progress_bar=false')
    con.execute(f"SET temp_directory='{out_dir / 'duckdb_tmp'}'")
    return con


def find_logs(logs_dir: Path) -> list[Path]:
    found = []
    for pattern in LOG_GLOBS:
        found.extend(logs_dir.glob(pattern))
    return sorted(found)


def ingest(
    logs_dir: Path,
    out_dir: Path,
    self_host: str = classify.DEFAULT_SELF_HOST,
    memory_limit: str = DEFAULT_MEMORY_LIMIT,
    threads: int | None = None,
    force: bool = False,
) -> Path:
    """Convert every log in ``logs_dir`` into Parquet; returns the dir."""
    parquet_dir = out_dir / PARQUET_DIRNAME
    parquet_dir.mkdir(parents=True, exist_ok=True)
    logs = find_logs(logs_dir)
    if not logs:
        raise FileNotFoundError(f'No *.log files in {logs_dir}')

    con = connect(out_dir, memory_limit, threads)
    select_sql = build_select_sql(self_host)
    self_referer_re = rf'^https?://{self_host}(/|$)'

    for log in logs:
        target = parquet_dir / f'{log.name.split(".log")[0]}.parquet'
        if target.exists() and not force:
            logger.info('Skipping %s (already ingested)', log.name)
            continue
        logger.info('Ingesting %s', log.name)
        tmp = target.with_suffix('.tmp')
        con.execute(
            f"COPY ({select_sql}) TO '{tmp}' "
            "(FORMAT PARQUET, COMPRESSION zstd)",
            [str(log), self_referer_re],
        )
        tmp.rename(target)
    return parquet_dir
