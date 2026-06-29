"""Persistent per-report state: which S3 keys have already been ingested.

State files are named YYYY-MM and contain one ingested S3 key per line.
Each report gets its own state directory so dedup is independent.
"""

from datetime import date, timedelta
from pathlib import Path


def load_ingested_keys(
    state_dir: Path,
    start_date: date,
    end_date: date,
) -> set[str]:
    """Load the set of S3 keys already ingested for the given date range."""
    months = set()
    current = start_date
    while current <= end_date:
        months.add(current.strftime('%Y-%m'))
        current += timedelta(days=1)

    ingested = set()
    for month in months:
        state_file = state_dir / month
        if not state_file.exists():
            continue
        with state_file.open() as f:
            for line in f:
                line = line.strip()
                if line:
                    ingested.add(line)
    return ingested


def mark_ingested(state_dir: Path, s3, key: str):
    """Append an S3 key to the state file for the month it belongs to."""
    state_dir.mkdir(parents=True, exist_ok=True)
    month = s3.date_from_key(key).strftime('%Y-%m')
    state_file = state_dir / month
    with state_file.open('a') as f:
        f.write(key + '\n')
