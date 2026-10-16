"""Download a range of Nginx logs from S3.

Objects are fetched for each day in the (inclusive) date range. By default
each S3 object is saved as its own file. Use --decompress to gunzip them, and
--squash-days N to concatenate them into N-day batches, e.g.:

    python download.py --from 2026-09-01 --to 2026-10-15 \\
        --outdir 6weeks --decompress --squash-days 7

Batch files are named <first-day>_<last-day>.log[.gz]. Without --decompress,
batches are concatenated gzip members, which gzip/zcat read as one stream.
"""

import argparse
import gzip
import logging
import sys
from datetime import date, timedelta
from pathlib import Path

from dotenv import load_dotenv

from common.s3 import S3Storage

load_dotenv(Path(__file__).parent / '.env')

LOG_FORMAT = '%(levelname)s: %(message)s'
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
logger = logging.getLogger(__name__)

SERVER_PATHS = {'prod': 'galaxy', 'dev': 'dev'}
LOG_TYPES = ('cleaned', 'raw', 'error', 'tool_runs', 'workflow_invocations')
DEFAULT_TYPE = 'cleaned'
DEFAULT_SERVER = 'prod'
PREFIX_TEMPLATE = '{server}/nginx-logs/{log_type}/'
GZ_SUFFIX = '.log.gz'
PLAIN_SUFFIX = '.log'


def build_prefix(server: str, log_type: str) -> str:
    """Return the S3 key prefix for a server and log type."""
    return PREFIX_TEMPLATE.format(
        server=SERVER_PATHS[server],
        log_type=log_type,
    )


def batch_windows(
    start: date,
    end: date,
    days: int,
) -> list[tuple[date, date]]:
    """Split [start, end] into inclusive windows of `days` days.

    The last window is clipped to `end`.
    """
    windows = []
    current = start
    while current <= end:
        last = min(current + timedelta(days=days - 1), end)
        windows.append((current, last))
        current = last + timedelta(days=1)
    return windows


def download(
    s3: S3Storage,
    start: date,
    end: date,
    outdir: Path,
    decompress: bool = False,
    squash_days: int | None = None,
) -> dict:
    """Download logs in [start, end] to outdir and return summary counts."""
    outdir.mkdir(parents=True, exist_ok=True)
    suffix = PLAIN_SUFFIX if decompress else GZ_SUFFIX
    windows = batch_windows(start, end, squash_days) if squash_days else []
    stats = {'objects': 0, 'files': set(), 'bytes': 0}

    for key in s3.iter_keys(start, end):
        day = s3.date_from_key(key)
        if squash_days:
            first, last = next(w for w in windows if w[0] <= day <= w[1])
            path = outdir / f'{first}_{last}{suffix}'
            # Batch files are rebuilt on each run: truncate on first write.
            mode = 'ab' if path in stats['files'] else 'wb'
        else:
            name = key.rsplit('/', 1)[-1].removesuffix(GZ_SUFFIX)
            path = outdir / f'{day}_{name}{suffix}'
            mode = 'wb'
            if path.exists():
                logger.info("Skipping existing file %s", path)
                continue

        data = s3.read_object(key)
        if decompress:
            data = gzip.decompress(data)
        with open(path, mode) as f:
            f.write(data)
        stats['objects'] += 1
        stats['bytes'] += len(data)
        stats['files'].add(path)

    stats['files'] = len(stats['files'])
    return stats


def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        '--from',
        dest='start',
        type=date.fromisoformat,
        required=True,
        help='First day to download (YYYY-MM-DD).',
    )
    parser.add_argument(
        '--to',
        dest='end',
        type=date.fromisoformat,
        required=True,
        help='Last day to download, inclusive (YYYY-MM-DD).',
    )
    parser.add_argument(
        '--outdir',
        type=Path,
        required=True,
        help='Directory to write files to (created if missing).',
    )
    parser.add_argument(
        '--decompress',
        action='store_true',
        help='Gunzip logs before writing.',
    )
    parser.add_argument(
        '--squash-days',
        type=int,
        help='Concatenate logs into batches of this many days.',
    )
    parser.add_argument(
        '--type',
        dest='log_type',
        choices=LOG_TYPES,
        default=DEFAULT_TYPE,
        help='Log type to download (default: %(default)s).',
    )
    parser.add_argument(
        '--server',
        choices=sorted(SERVER_PATHS),
        default=DEFAULT_SERVER,
        help='Galaxy server to download logs for (default: %(default)s).',
    )
    args = parser.parse_args()

    if args.start > args.end:
        parser.error('--from must not be after --to')
    if args.squash_days is not None and args.squash_days < 1:
        parser.error('--squash-days must be at least 1')

    prefix = build_prefix(args.server, args.log_type)
    logger.info(
        "Downloading %s from %s to %s", prefix, args.start, args.end)
    stats = download(
        S3Storage(prefix=prefix),
        args.start,
        args.end,
        args.outdir,
        decompress=args.decompress,
        squash_days=args.squash_days,
    )
    if not stats['objects']:
        logger.warning("No objects found for %s", prefix)
        sys.exit(1)
    logger.info(
        "Done: %d object(s) -> %d file(s), %d bytes in %s",
        stats['objects'], stats['files'], stats['bytes'], args.outdir,
    )


if __name__ == '__main__':
    main()
