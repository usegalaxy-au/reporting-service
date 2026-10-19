"""Replay parsed logs through generated fail2ban filters and jails.

The failregex/ignoreregex are loaded from the .conf files so what is
tested is exactly what would be deployed. Matching runs in DuckDB (RE2);
the ban simulation follows fail2ban semantics: ``maxretry`` failures
inside ``findtime`` bans the IP for ``bantime``; failures while banned
are ignored and counting restarts once the ban expires.
"""

import csv
import ipaddress
import logging
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from analysis import classify, filters
from analysis.ingest import PARQUET_DIRNAME
from analysis.queries import MALICIOUS_SQL, Analysis

logger = logging.getLogger(__name__)

HOST_GROUP = r'(\S+)'
REPEAT_MIN_WEEKS = 2
MONTH_FORMAT = '%Y-%m'


def _sql_regexes(regexes: list[str]) -> str:
    quoted = [classify._sql_quote(r.replace('<HOST>', HOST_GROUP))
              for r in regexes]
    return ' OR '.join(f'regexp_matches(line, {q})' for q in quoted)


def load_ignore_networks(jail_path: Path) -> list:
    import configparser
    parser = configparser.RawConfigParser()
    parser.read(jail_path)
    networks = []
    for section in parser.sections():
        for token in parser.get(section, 'ignoreip', fallback='').split():
            try:
                networks.append(ipaddress.ip_network(token, strict=False))
            except ValueError:
                logger.warning('Bad ignoreip entry: %s', token)
    return networks


def is_ignored(ip: str, networks: list) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in net for net in networks)


def simulate(
    events: list[tuple[str, datetime]],
    maxretry: int,
    findtime: int,
    bantime: int,
) -> list[tuple[str, datetime, datetime, int]]:
    """Return bans as (ip, start, end, failures) from sorted events."""
    window = timedelta(seconds=findtime)
    ban_length = timedelta(seconds=bantime)
    per_ip = defaultdict(list)
    for ip, ts in events:
        per_ip[ip].append(ts)

    bans = []
    for ip, times in per_ip.items():
        recent: list[datetime] = []
        banned_until = None
        for ts in times:
            if banned_until and ts < banned_until:
                continue
            banned_until = None
            recent = [t for t in recent if ts - t <= window]
            recent.append(ts)
            if len(recent) >= maxretry:
                banned_until = ts + ban_length
                bans.append((ip, ts, banned_until, len(recent)))
                recent = []
    return bans


def repeat_offenders(
    bans: list[tuple],
    min_weeks: int = REPEAT_MIN_WEEKS,
) -> list[dict]:
    """IPs banned in at least ``min_weeks`` distinct ISO weeks.

    ``bans`` rows are (jail, ip, start, end, failures). Many repeat
    offenders is the case for a fail2ban ``recidive`` jail.
    """
    per_ip = defaultdict(list)
    for jail, ip, start, _end, _failures in bans:
        per_ip[ip].append((jail, start))
    rows = []
    for ip, entries in per_ip.items():
        weeks = {start.isocalendar()[:2] for _, start in entries}
        if len(weeks) < min_weeks:
            continue
        starts = [start for _, start in entries]
        rows.append({
            'ip': ip,
            'subnet24': ip.rsplit('.', 1)[0] + '.0/24' if '.' in ip else ip,
            'bans': len(entries),
            'weeks_banned': len(weeks),
            'jails': ' '.join(sorted({jail for jail, _ in entries})),
            'first_ban': min(starts),
            'last_ban': max(starts),
        })
    return sorted(rows, key=lambda r: (-r['weeks_banned'], -r['bans']))


def _write_rows(path: Path, rows: list[dict], columns: list[str]) -> Path:
    with open(path, 'w', newline='') as fh:
        writer = csv.DictWriter(fh, fieldnames=columns, restval='')
        writer.writeheader()
        writer.writerows(rows)
    logger.info('Wrote %s', path)
    return path


def run(
    analysis: Analysis,
    fail2ban_dir: Path = filters.FAIL2BAN_DIR,
) -> list[Path]:
    """Backtest every jail; writes summary, bans and false positives."""
    stale = filters.stale_files(fail2ban_dir)
    if stale:
        raise RuntimeError(
            'Filter files are out of date with analysis/classify.py; run '
            '`python spam_analysis.py filters`: '
            + ', '.join(p.name for p in stale))
    analysis._ensure_ip_stats()
    con = analysis.con
    jail_path = fail2ban_dir / filters.JAIL_FILENAME
    jails = filters.load_jails(jail_path)
    ignore_nets = load_ignore_networks(jail_path)
    # Loopback can't be banned (see jail ignoreip), so it isn't counted.
    total_malicious = con.execute(
        f'SELECT count(*) FROM traffic WHERE {MALICIOUS_SQL} '
        f"AND NOT regexp_matches(ip, '{classify.LOOPBACK_RE}')"
    ).fetchone()[0]

    summary, all_bans = [], []
    for name, jail in jails.items():
        fail, ignore = filters.load_filter(
            fail2ban_dir / filters.FILTER_DIRNAME / f"{jail['filter']}.conf")
        where = f'({_sql_regexes(fail)})'
        if ignore:
            where += f' AND NOT ({_sql_regexes(ignore)})'
        # Crawlers are out of scope, so they're neither banned nor counted.
        events = [
            (ip, ts) for ip, ts in con.execute(
                f'SELECT ip, ts FROM traffic WHERE {where} '
                'ORDER BY ip, ts').fetchall()
            if not is_ignored(ip, ignore_nets)
        ]
        bans = simulate(
            events, jail['maxretry'], jail['findtime'], jail['bantime'])
        logger.info('%s: %d matches, %d bans', name, len(events), len(bans))
        all_bans.extend((name, *b) for b in bans)
        summary.append({'jail': name, 'matched_requests': len(events),
                        'matched_ips': len({e[0] for e in events}),
                        'bans': len(bans),
                        'banned_ips': len({b[0] for b in bans})})

    paths = _report(analysis, summary, all_bans, total_malicious)
    return paths


def _report(analysis, summary, all_bans, total_malicious) -> list[Path]:
    con = analysis.con
    out = analysis.out_dir
    con.execute(
        'CREATE OR REPLACE TABLE bans (jail VARCHAR, ip VARCHAR, '
        'ban_start TIMESTAMP, ban_end TIMESTAMP, failures INTEGER)')
    if all_bans:
        con.executemany('INSERT INTO bans VALUES (?, ?, ?, ?, ?)', all_bans)

    # Every request made by an IP while it would have been banned.
    con.execute(f"""
        CREATE OR REPLACE TABLE blocked AS
        SELECT b.jail, t.ip, t.ts, t.status, t.api_kind, t.self_referer,
               {MALICIOUS_SQL} AS malicious
        FROM bans b JOIN traffic t
          ON t.ip = b.ip AND t.ts >= b.ban_start AND t.ts < b.ban_end
    """)
    per_jail = {r[0]: r[1:] for r in con.execute("""
        SELECT jail, count(*), sum(malicious::INT),
               sum((NOT malicious AND api_kind = 'legit_api')::INT),
               sum((NOT malicious AND status < 400)::INT)
        FROM blocked GROUP BY jail""").fetchall()}
    for row in summary:
        blocked, mal, legit_api, ok = per_jail.get(
            row['jail'], (0, 0, 0, 0))
        row.update({
            'blocked_requests': blocked,
            'blocked_malicious': mal or 0,
            'blocked_legit_api': legit_api or 0,
            'blocked_non_malicious_ok': ok or 0,
        })

    # EXISTS counts each request once even if several jails ban the IP
    # (and, unlike DISTINCT, keeps same-second requests separate).
    con.execute(f"""
        CREATE OR REPLACE TABLE blocked_once AS
        SELECT ip, ts, api_kind, {MALICIOUS_SQL} AS malicious
        FROM traffic t
        WHERE ip IN (SELECT ip FROM bans)
          AND EXISTS (SELECT 1 FROM bans b WHERE b.ip = t.ip
                      AND t.ts >= b.ban_start AND t.ts < b.ban_end)
    """)
    combined = con.execute("""
        SELECT count(*), sum(malicious::INT),
               sum((NOT malicious AND api_kind = 'legit_api')::INT)
        FROM blocked_once
    """).fetchone()
    summary.append({
        'jail': 'ALL (combined)',
        'banned_ips': con.execute(
            'SELECT count(DISTINCT ip) FROM bans').fetchone()[0],
        'blocked_requests': combined[0],
        'blocked_malicious': combined[1] or 0,
        'blocked_legit_api': combined[2] or 0,
        'malicious_total': total_malicious,
    })

    paths = [_write_rows(
        out / 'backtest_summary.csv', summary,
        ['jail', 'matched_requests', 'matched_ips', 'bans', 'banned_ips',
         'blocked_requests', 'blocked_malicious', 'blocked_legit_api',
         'blocked_non_malicious_ok', 'malicious_total'])]
    paths.append(_by_month(analysis))
    paths.append(_write_rows(
        out / 'backtest_repeat_offenders.csv', repeat_offenders(all_bans),
        ['ip', 'subnet24', 'bans', 'weeks_banned', 'jails', 'first_ban',
         'last_ban']))

    paths.append(analysis._csv('backtest_bans.csv', """
        SELECT b.jail, b.ip, b.ban_start, b.failures,
               s.requests, s.malicious, s.good, s.is_baseline
        FROM bans b JOIN ip_stats s USING (ip)
        ORDER BY b.ban_start"""))
    paths.append(analysis._csv('backtest_false_positives.csv', """
        SELECT b.jail, b.ip, b.ban_start, b.failures, s.requests,
               s.malicious, s.good, s.legit_api
        FROM bans b JOIN ip_stats s USING (ip)
        WHERE s.is_baseline OR s.good > 0
        ORDER BY s.good DESC"""))
    return paths


def _by_month(analysis: Analysis) -> Path:
    """Per-jail and combined ban/block figures for each calendar month."""
    month = f"strftime({{col}}, '{MONTH_FORMAT}')"
    ban_month = month.format(col='b.ban_start')
    loopback = classify.LOOPBACK_RE
    return analysis._csv('backtest_by_month.csv', f"""
        WITH per_jail_bans AS (
            SELECT {ban_month} AS month, b.jail, count(*) AS bans,
                count(DISTINCT b.ip) AS banned_ips,
                count(DISTINCT b.ip) FILTER (WHERE s.is_baseline)
                    AS baseline_ips_banned
            FROM bans b JOIN ip_stats s USING (ip) GROUP BY 1, 2
        ), per_jail_blocked AS (
            SELECT {month.format(col='ts')} AS month, jail,
                sum(malicious::INT) AS blocked_malicious,
                sum((NOT malicious AND api_kind = 'legit_api')::INT)
                    AS blocked_legit_api
            FROM blocked GROUP BY 1, 2
        ), all_bans AS (
            SELECT {ban_month} AS month, count(*) AS bans,
                count(DISTINCT b.ip) AS banned_ips,
                count(DISTINCT b.ip) FILTER (WHERE s.is_baseline)
                    AS baseline_ips_banned
            FROM bans b JOIN ip_stats s USING (ip) GROUP BY 1
        ), all_blocked AS (
            SELECT {month.format(col='ts')} AS month,
                sum(malicious::INT) AS blocked_malicious,
                sum((NOT malicious AND api_kind = 'legit_api')::INT)
                    AS blocked_legit_api
            FROM blocked_once GROUP BY 1
        ), malicious AS (
            SELECT {month.format(col='ts')} AS month,
                count(*) AS malicious_total
            FROM traffic
            WHERE {MALICIOUS_SQL} AND NOT regexp_matches(ip, '{loopback}')
            GROUP BY 1
        )
        SELECT month, jail, coalesce(bans, 0) AS bans,
            coalesce(banned_ips, 0) AS banned_ips,
            coalesce(baseline_ips_banned, 0) AS baseline_ips_banned,
            coalesce(blocked_malicious, 0) AS blocked_malicious,
            coalesce(blocked_legit_api, 0) AS blocked_legit_api,
            NULL AS malicious_total, NULL AS pct_malicious_blocked
        FROM per_jail_bans FULL JOIN per_jail_blocked USING (month, jail)
        UNION ALL
        SELECT month, 'ALL (combined)', coalesce(bans, 0),
            coalesce(banned_ips, 0), coalesce(baseline_ips_banned, 0),
            coalesce(blocked_malicious, 0), coalesce(blocked_legit_api, 0),
            malicious_total,
            round(100.0 * coalesce(blocked_malicious, 0)
                  / malicious_total, 1)
        FROM malicious
            LEFT JOIN all_bans USING (month)
            LEFT JOIN all_blocked USING (month)
        ORDER BY month, jail""")


def match_counts(
    analysis: Analysis,
    source: str,
    fail2ban_dir: Path = filters.FAIL2BAN_DIR,
) -> Path:
    """Raw per-filter match counts for one ingested log file.

    Mirrors ``fail2ban-regex <log> <filter>``: every line is tested, with
    no crawler or loopback exclusion and no jail (maxretry) logic, so the
    totals should equal fail2ban-regex's "matched" counts.
    """
    parquet = analysis.out_dir / PARQUET_DIRNAME / f'{source}.parquet'
    if not parquet.exists():
        raise FileNotFoundError(f'No ingested source {parquet}')
    con = analysis.con
    rows = []
    jails = filters.load_jails(fail2ban_dir / filters.JAIL_FILENAME)
    for name, jail in jails.items():
        fail, ignore = filters.load_filter(
            fail2ban_dir / filters.FILTER_DIRNAME / f"{jail['filter']}.conf")
        ignored = f'({_sql_regexes(ignore)})' if ignore else 'false'
        total, matched, ignored_count = con.execute(f"""
            SELECT count(*),
                sum(({_sql_regexes(fail)}) AND NOT {ignored})::BIGINT,
                sum(({_sql_regexes(fail)}) AND {ignored})::BIGINT
            FROM read_parquet('{parquet}')""").fetchone()
        rows.append({'filter': jail['filter'], 'lines': total,
                     'matched': matched or 0,
                     'ignored': ignored_count or 0})
        logger.info('%s: %d of %d lines matched', jail['filter'],
                    matched or 0, total)
    return _write_rows(
        analysis.out_dir / f'matchcount_{source}.csv', rows,
        ['filter', 'lines', 'matched', 'ignored'])
