"""Unified entrypoint for Galaxy reporting collectors.

Usage:
    report.py <report> [--start YYYY-MM-DD] [--end YYYY-MM-DD] [--dry]

    <report> is one of the registered report names (see reports/).
    --start defaults to LOOKBACK_DAYS days ago, --end defaults to today.
    Both are inclusive. State files dedup keys already ingested, so the
    redundant LIST calls on the lookback window are cheap; the lookback
    exists to pick up Vector batches that flush to S3 hours or days
    after the events they contain.
"""

import argparse
from datetime import date, timedelta
from pathlib import Path

from dotenv import load_dotenv

from common.log import setup_logging
from common.runner import run
from reports import REGISTRY

LOOKBACK_DAYS = 7


def parse_args():
    parser = argparse.ArgumentParser(
        description="Collect reporting data from S3 nginx logs.",
    )
    parser.add_argument(
        'report',
        choices=sorted(REGISTRY),
        help="Which report to run.",
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
    parser.add_argument(
        '--dry',
        action='store_true',
        help="Print line protocol to stdout instead of writing to InfluxDB.",
    )
    args = parser.parse_args()
    today = date.today()
    args.start = args.start or today - timedelta(days=LOOKBACK_DAYS)
    args.end = args.end or today
    if args.start > args.end:
        parser.error(
            f"--start ({args.start}) is after --end ({args.end})")
    return args


def main():
    load_dotenv(Path(__file__).parent / '.env')
    setup_logging()
    args = parse_args()
    run(REGISTRY[args.report], args.start, args.end, args.dry)


if __name__ == '__main__':
    main()
