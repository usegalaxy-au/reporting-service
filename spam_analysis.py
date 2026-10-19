"""Analyse raw nginx logs to inform fail2ban rules against spam traffic.

Usage:
    python spam_analysis.py parse   --logs DIR --out DIR
    python spam_analysis.py profile --out DIR   # genuine-traffic baseline
    python spam_analysis.py patterns --out DIR  # malicious patterns
    python spam_analysis.py ranges  --out DIR   # spam-only / mixed ranges
    python spam_analysis.py filters             # regenerate fail2ban files
    python spam_analysis.py backtest --out DIR  # replay logs through them
    python spam_analysis.py report --out DIR    # findings.md + chart
    python spam_analysis.py matchcount --out DIR --source NAME
                                  # compare with fail2ban-regex
    python spam_analysis.py all --logs DIR --out DIR
"""

import argparse
import logging
from pathlib import Path

from analysis import backtest, classify, filters, report
from analysis.ingest import DEFAULT_MEMORY_LIMIT, ingest
from analysis.queries import Analysis

LOG_FORMAT = '%(levelname)s: %(message)s'
ANALYSES = ('profile', 'patterns', 'ranges')
COMMANDS = (
    'parse', *ANALYSES, 'filters', 'backtest', 'report', 'matchcount',
    'all')


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument(
        'command', choices=COMMANDS,
        help='Step to run; "all" runs parse then every analysis.')
    parser.add_argument('--logs', type=Path, help='Dir of raw *.log files')
    parser.add_argument(
        '--out', type=Path, default=None,
        help='Output dir (Parquet + CSVs), e.g. analysis/output/<sample>')
    parser.add_argument(
        '--self-host', default=classify.DEFAULT_SELF_HOST,
        help='Regex of the server hostname, to spot self-referers.')
    parser.add_argument(
        '--force', action='store_true', help='Re-ingest existing Parquet.')
    parser.add_argument(
        '--source',
        help='matchcount: ingested file name, i.e. the Parquet stem '
             '(e.g. 2026-08-03_2026-08-03).')
    parser.add_argument(
        '--memory-limit', default=DEFAULT_MEMORY_LIMIT,
        help='DuckDB memory limit (default: %(default)s); DuckDB spills '
             'to <out>/duckdb_tmp beyond it.')
    parser.add_argument(
        '--threads', type=int, default=None,
        help='DuckDB worker threads (default: one per core).')
    return parser.parse_args()


def main():
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
    if args.command == 'filters':
        for path in filters.write_all():
            logging.info('Wrote %s', path)
        return
    if not args.out:
        raise SystemExit('--out is required')
    args.out.mkdir(parents=True, exist_ok=True)

    if args.command in ('parse', 'all'):
        if not args.logs:
            raise SystemExit('--logs is required for parse')
        ingest(args.logs, args.out, self_host=args.self_host,
               memory_limit=args.memory_limit, threads=args.threads,
               force=args.force)

    if args.command == 'report':
        report.write(args.out)
        return
    if args.command == 'matchcount':
        if not args.source:
            raise SystemExit('--source is required for matchcount')
        backtest.match_counts(
            Analysis(args.out, memory_limit=args.memory_limit,
                     threads=args.threads),
            args.source)
        return

    commands = ANALYSES if args.command == 'all' else (args.command,)
    commands = [c for c in commands if c in ANALYSES]
    if commands or args.command in ('backtest', 'all'):
        analysis = Analysis(
            args.out, memory_limit=args.memory_limit, threads=args.threads)
        for command in commands:
            getattr(analysis, command)()
        if args.command in ('backtest', 'all'):
            backtest.run(analysis)
    if args.command == 'all':
        report.write(args.out)


if __name__ == '__main__':
    main()
