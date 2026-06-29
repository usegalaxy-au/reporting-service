"""Generic per-key ingest loop shared by all reports."""

import logging
import os
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Callable, Optional

from sqlalchemy import create_engine

sys.path.insert(0, str(Path(__file__).parent.parent))
from s3 import S3Storage  # noqa: E402

from common.domains import load_domain_map  # noqa: E402
from common.influx import write_to_influxdb  # noqa: E402
from common.state import load_ingested_keys, mark_ingested  # noqa: E402

logger = logging.getLogger(__name__)


@dataclass
class Report:
    """A single report definition: how to parse, enrich, and emit one
    type of data point."""
    name: str
    s3_prefix_env: str
    measurement: str
    state_dir: Path
    parse_record: Callable[[dict], Optional[dict]]
    build_points: Callable[..., list[str]]


def run(report: Report, start_date: date, end_date: date, dry: bool) -> None:
    s3 = S3Storage(prefix=os.environ[report.s3_prefix_env])
    ingested = load_ingested_keys(report.state_dir, start_date, end_date)
    domain_map = load_domain_map(report.state_dir / 'domains.json')
    engine = create_engine(os.environ['GALAXY_DATABASE_URL'])

    with engine.connect() as conn:
        for key in s3.iter_keys(start_date, end_date):
            if key in ingested:
                logger.debug("Skipping already-ingested key: %s", key)
                continue

            data_points = []
            for record in s3.read_records(key):
                parsed = report.parse_record(record)
                if not parsed:
                    continue
                data_points.extend(
                    report.build_points(
                        conn=conn,
                        parsed=parsed,
                        domain_map=domain_map,
                        measurement=report.measurement,
                    )
                )

            if data_points:
                if dry:
                    for line in data_points:
                        print(line)
                else:
                    write_to_influxdb(data_points)
                    mark_ingested(report.state_dir, s3, key)
            else:
                logger.warning(
                    "No matching records found in %s — "
                    "key not marked ingested",
                    key,
                )
