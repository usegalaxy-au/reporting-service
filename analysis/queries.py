"""DuckDB analyses over the Parquet produced by ``analysis.ingest``.

Each public method writes one or more CSV files into the output dir and
returns the paths. Crawlers are excluded from every analysis; legitimate
``/api`` traffic is never counted as malicious.
"""

import logging
from pathlib import Path

from analysis import classify
from analysis.ingest import PARQUET_DIRNAME, connect

logger = logging.getLogger(__name__)

DEFAULT_MEMORY_LIMIT = '6GB'
# An IP is "baseline" (known genuine) with this many good-signal requests.
MIN_BASELINE_GOOD_REQUESTS = 20
# Ranges need this many malicious requests to be listed at all.
MIN_RANGE_MALICIOUS = 20
# Share of a range's requests that must be malicious to call it spam-only.
SPAM_ONLY_FRACTION = 0.9
ERROR_RATE_THRESHOLDS = (3, 5, 10, 20, 50, 100)
FLOOD_TOP_N = 25
MINING_MIN_IPS = 3
MINING_TOP_N = 300
# Window lengths (seconds) for the per-IP error-rate analysis.
ERROR_WINDOWS = {'1min': 60, '10min': 600}
FLOOD_EXEMPT = classify.FLOOD_EXEMPT_PATH_RE.replace("'", "''")

MALICIOUS_SQL = '(scanner_match IS NOT NULL OR ua_tool)'
GOOD_SQL = (
    f"(NOT {MALICIOUS_SQL} AND status < 400 "
    "AND (api_kind = 'legit_api' OR self_referer))")
NORMALISED_PATH_SQL = (
    "regexp_replace(regexp_replace(path, '[0-9a-f]{16,}', '<id>', 'g'), "
    "'[0-9]+', '<n>', 'g')")
RANGE_LEVELS = ('ip', 'subnet24', 'subnet16')


class Analysis:
    def __init__(
        self,
        out_dir: Path,
        memory_limit: str = DEFAULT_MEMORY_LIMIT,
        threads: int | None = None,
    ):
        self.out_dir = out_dir
        self.con = connect(out_dir, memory_limit, threads)
        glob = out_dir / PARQUET_DIRNAME / '*.parquet'
        self.con.execute(
            f"CREATE VIEW logs AS SELECT * FROM read_parquet('{glob}') "
            "WHERE ip IS NOT NULL")
        # Crawlers are out of scope for now.
        self.con.execute(
            "CREATE VIEW traffic AS SELECT * FROM logs WHERE NOT is_crawler")
        self._ip_stats_ready = False

    # -- helpers ---------------------------------------------------------

    def _csv(self, name: str, query: str) -> Path:
        path = self.out_dir / name
        self.con.execute(
            f"COPY ({query}) TO '{path}' (HEADER, DELIMITER ',')")
        logger.info('Wrote %s', path)
        return path

    def _ensure_ip_stats(self):
        """Per-IP table with malicious/good counts and baseline flag."""
        if self._ip_stats_ready:
            return
        self.con.execute(f"""
            CREATE TABLE ip_stats AS
            SELECT ip, any_value(subnet24) AS subnet24,
                any_value(subnet16) AS subnet16,
                count(*) AS requests,
                count(DISTINCT CAST(ts AS DATE)) AS days,
                sum({MALICIOUS_SQL}::INT) AS malicious,
                sum({GOOD_SQL}::INT) AS good,
                sum((is_ui_poll AND self_referer)::INT) AS ui_poll,
                sum((api_kind = 'legit_api')::INT) AS legit_api,
                sum(is_monitor::INT) AS monitor,
                sum((ua_empty)::INT) AS empty_ua,
                min(ts) AS first_seen, max(ts) AS last_seen,
                (sum({GOOD_SQL}::INT) >= {MIN_BASELINE_GOOD_REQUESTS}
                 OR sum(is_monitor::INT) > 0) AS is_baseline
            FROM traffic GROUP BY ip
        """)
        self._ip_stats_ready = True

    # -- step 1: genuine-traffic baseline -------------------------------

    def profile(self) -> list[Path]:
        self._ensure_ip_stats()
        paths = [
            self._csv('baseline_ips.csv', """
                SELECT * FROM ip_stats WHERE is_baseline
                ORDER BY requests DESC"""),
            self._csv('status_mix_by_category.csv', f"""
                SELECT CASE WHEN is_monitor THEN 'monitor'
                            WHEN {MALICIOUS_SQL} THEN 'malicious'
                            WHEN api_kind = 'legit_api' THEN 'legit_api'
                            WHEN is_ui_poll THEN 'ui_poll'
                            ELSE 'other' END AS category,
                       status, count(*) AS requests,
                       count(DISTINCT ip) AS ips
                FROM traffic GROUP BY ALL ORDER BY 1, 3 DESC"""),
            self._csv('baseline_non_api_4xx_paths.csv', f"""
                SELECT {NORMALISED_PATH_SQL} AS path_pattern, status,
                       count(*) AS requests, count(DISTINCT ip) AS ips,
                       any_value(target) AS example
                FROM traffic JOIN ip_stats USING (ip)
                WHERE is_baseline AND api_kind != 'legit_api'
                  AND status BETWEEN 400 AND 499
                GROUP BY ALL ORDER BY requests DESC LIMIT 200"""),
            self._csv('legit_api_ua_families.csv', """
                SELECT coalesce(regexp_extract(ua, '^([A-Za-z0-9_.-]+)', 1),
                                '(empty)') AS ua_family,
                       count(*) AS requests, count(DISTINCT ip) AS ips,
                       round(100.0 * sum((status >= 400)::INT)
                             / count(*), 2) AS pct_4xx
                FROM traffic WHERE api_kind = 'legit_api'
                GROUP BY ALL ORDER BY requests DESC LIMIT 50"""),
            self._csv('allowlist_candidates.csv', f"""
                SELECT ip, subnet24, requests, good, monitor, days,
                    CASE WHEN regexp_matches(ip, '{classify.LOOPBACK_RE}')
                         THEN 'loopback: proxied traffic, real IP lost'
                         WHEN monitor > 0 THEN 'monitoring canary'
                         ELSE 'high-volume genuine user' END AS reason
                FROM ip_stats
                WHERE regexp_matches(ip, '{classify.LOOPBACK_RE}')
                   OR (malicious = 0 AND (monitor > 0
                       OR (is_baseline AND requests >= 1000)))
                ORDER BY requests DESC"""),
        ]
        return paths

    # -- step 2: malicious pattern catalogue ----------------------------

    def patterns(self) -> list[Path]:
        self._ensure_ip_stats()
        category = (
            "coalesce(scanner_match, CASE WHEN ua_tool THEN 'ua_tool' END)")
        paths = [
            self._csv('patterns.csv', f"""
                SELECT {category} AS category,
                    count(*) AS requests, count(DISTINCT ip) AS ips,
                    round(100.0 * sum((status = 404)::INT) / count(*), 1)
                        AS pct_404,
                    round(100.0 * sum((status BETWEEN 200 AND 299)::INT)
                          / count(*), 1) AS pct_2xx,
                    round(100.0 * sum((status BETWEEN 300 AND 399)::INT)
                          / count(*), 1) AS pct_3xx,
                    count(DISTINCT ip) FILTER (WHERE is_baseline)
                        AS baseline_ips,
                    sum(is_baseline::INT) AS baseline_requests,
                    count(DISTINCT CAST(ts AS DATE)) AS days,
                    any_value(target) AS example_target
                FROM traffic JOIN ip_stats USING (ip)
                WHERE {MALICIOUS_SQL}
                GROUP BY 1 ORDER BY requests DESC"""),
            self._csv('patterns_by_month.csv', f"""
                SELECT strftime(ts, '%Y-%m') AS month,
                    {category} AS category,
                    count(*) AS requests, count(DISTINCT ip) AS ips,
                    count(DISTINCT ip) FILTER (WHERE is_baseline)
                        AS baseline_ips
                FROM traffic JOIN ip_stats USING (ip)
                WHERE {MALICIOUS_SQL}
                GROUP BY 1, 2 ORDER BY 1, requests DESC"""),
            self._csv('pattern_baseline_hits.csv', f"""
                SELECT ip, {category} AS category, count(*) AS requests,
                       min(status) AS min_status, any_value(target) AS example
                FROM traffic JOIN ip_stats USING (ip)
                WHERE {MALICIOUS_SQL} AND is_baseline
                GROUP BY 1, 2 ORDER BY requests DESC LIMIT 500"""),
            self._csv('unmatched_error_paths.csv', f"""
                SELECT {NORMALISED_PATH_SQL} AS path_pattern,
                    count(*) AS requests, count(DISTINCT ip) AS ips,
                    count(DISTINCT ip) FILTER (WHERE is_baseline)
                        AS baseline_ips,
                    round(100.0 * sum((status = 404)::INT) / count(*), 1)
                        AS pct_404,
                    any_value(target) AS example
                FROM traffic JOIN ip_stats USING (ip)
                WHERE NOT {MALICIOUS_SQL} AND api_kind != 'legit_api'
                  AND status BETWEEN 400 AND 499
                GROUP BY 1 HAVING count(DISTINCT ip) >= {MINING_MIN_IPS}
                ORDER BY ips DESC, requests DESC LIMIT {MINING_TOP_N}"""),
            self._csv('user_agents.csv', """
                SELECT CASE WHEN ua_empty THEN 'empty'
                            WHEN ua_tool THEN 'attack_tool'
                            WHEN ua_generic_lib THEN 'generic_lib'
                            ELSE 'browser_like' END AS ua_class,
                    api_kind, count(*) AS requests,
                    count(DISTINCT ip) AS ips,
                    round(100.0 * sum((status < 400)::INT) / count(*), 1)
                        AS pct_ok,
                    round(100.0 * sum((scanner_match IS NOT NULL)::INT)
                          / count(*), 1) AS pct_scanner_path,
                    count(DISTINCT ip) FILTER (WHERE is_baseline)
                        AS baseline_ips
                FROM traffic JOIN ip_stats USING (ip)
                GROUP BY ALL ORDER BY requests DESC"""),
            self._csv('methods.csv', """
                SELECT coalesce(method, '(malformed)') AS method,
                    count(*) AS requests, count(DISTINCT ip) AS ips,
                    any_value(substr(request, 1, 60)) AS example
                FROM traffic GROUP BY 1 ORDER BY requests DESC"""),
        ]
        paths.append(self._error_rates())
        paths.extend(self._floods())
        return paths

    def _error_rates(self) -> Path:
        """Max non-API 4xx per IP per window vs. the baseline's peak."""
        selects = []
        for label, seconds in ERROR_WINDOWS.items():
            self.con.execute(f"""
                CREATE OR REPLACE TABLE err_{label} AS
                SELECT ip, epoch(ts)::BIGINT // {seconds} AS bucket,
                       count(*) AS errors,
                       sum((scanner_match IS NOT NULL)::INT) AS scanner_hits
                FROM traffic
                WHERE status BETWEEN 400 AND 499
                  AND api_kind != 'legit_api'
                  AND NOT regexp_matches(path, '{FLOOD_EXEMPT}')
                GROUP BY ip, bucket""")
            for threshold in ERROR_RATE_THRESHOLDS:
                selects.append(f"""
                    SELECT '{label}' AS win, {threshold} AS threshold,
                        count(DISTINCT ip) AS ips_over,
                        count(DISTINCT ip) FILTER (WHERE is_baseline)
                            AS baseline_ips_over,
                        count(DISTINCT ip) FILTER (WHERE scanner_hits > 0)
                            AS ips_with_scanner_hits,
                        count(DISTINCT ip) FILTER (
                            WHERE scanner_hits = 0 AND NOT is_baseline)
                            AS ips_over_no_scanner_not_baseline
                    FROM err_{label} JOIN ip_stats USING (ip)
                    WHERE errors >= {threshold}""")
        return self._csv(
            'error_rate_thresholds.csv',
            ' UNION ALL '.join(selects) + ' ORDER BY win, threshold')

    def _floods(self) -> list[Path]:
        return [
            self._csv('minute_stats.csv', f"""
                SELECT date_trunc('minute', ts) AS minute,
                    count(*) AS requests, count(DISTINCT ip) AS ips,
                    sum({MALICIOUS_SQL}::INT) AS malicious,
                    sum((status = 502)::INT) AS status_502,
                    sum((status BETWEEN 400 AND 499)::INT) AS status_4xx
                FROM traffic GROUP BY 1 ORDER BY 1"""),
            self._csv('flood_minutes.csv', f"""
                WITH m AS (
                    SELECT date_trunc('minute', ts) AS minute, ip, subnet24,
                        {MALICIOUS_SQL} AS mal
                    FROM traffic WHERE {MALICIOUS_SQL})
                SELECT minute, count(*) AS malicious,
                    count(DISTINCT ip) AS ips,
                    count(DISTINCT subnet24) AS subnets,
                    mode(ip) AS top_ip, mode(subnet24) AS top_subnet24
                FROM m GROUP BY 1 ORDER BY malicious DESC
                LIMIT {FLOOD_TOP_N}"""),
        ]

    # -- step 3: spam-only and mixed ranges -----------------------------

    def ranges(self) -> list[Path]:
        self._ensure_ip_stats()
        unions = []
        for level in RANGE_LEVELS:
            unions.append(f"""
                SELECT '{level}' AS level, {level} AS range,
                    sum(requests) AS requests, sum(malicious) AS malicious,
                    sum(good) AS good, count(*) AS ips,
                    sum(is_baseline::INT) AS baseline_ips,
                    sum((malicious > 0)::INT) AS malicious_ips,
                    max(days) AS days_active,
                    round(1.0 * sum(malicious) / sum(requests), 3)
                        AS malicious_fraction
                FROM ip_stats
                WHERE NOT regexp_matches(ip, '{classify.LOOPBACK_RE}')
                GROUP BY {level}
                HAVING sum(malicious) >= {MIN_RANGE_MALICIOUS}""")
        self.con.execute(
            'CREATE OR REPLACE TABLE ranges AS '
            + ' UNION ALL '.join(unions))
        return [
            self._csv('spam_ranges.csv', f"""
                SELECT * FROM ranges
                WHERE good = 0 AND baseline_ips = 0
                  AND malicious_fraction >= {SPAM_ONLY_FRACTION}
                ORDER BY level, malicious DESC"""),
            self._csv('mixed_ranges.csv', """
                SELECT * FROM ranges WHERE good > 0
                ORDER BY level, malicious DESC"""),
        ]
