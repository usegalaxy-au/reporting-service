"""Fixtures for integration tests.

- Boots a Postgres testcontainer (session-scoped) and seeds it from the
  `\\copy` TSVs in `ignore/tool-db-test/`.
- Pulls the pinned-day S3 records once per session and exposes them via
  a FakeS3 the tests can mutate to inject synthetic records.
- Per-test temp `state_dir` so dedup never bleeds across tests.
"""

import os
import shutil
from datetime import date
from pathlib import Path

import psycopg2
import pytest
from dotenv import load_dotenv
from sqlalchemy import create_engine
from testcontainers.postgres import PostgresContainer

# Load .env early so S3_PREFIX_TOOL_RUNS etc. are available at import time.
load_dotenv(Path(__file__).parent.parent.parent / '.env')

REPO_ROOT = Path(__file__).parent.parent.parent
FIXTURES_DIR = Path(__file__).parent / 'fixtures'
DB_FIXTURES_DIR = REPO_ROOT / 'tests' / 'data'
SCHEMA_SQL = (FIXTURES_DIR / 'schema.sql').read_text()

# Pinned date matching the user/job fixtures.
PINNED_DATE = date(2026, 6, 25)


@pytest.fixture(scope='session')
def pg_url():
    """Boot Postgres, load schema + fixtures, return SQLAlchemy URL."""
    users_tsv = DB_FIXTURES_DIR / 'users.tsv'
    jobs_tsv = DB_FIXTURES_DIR / 'jobs.tsv'
    if not (users_tsv.exists() and jobs_tsv.exists()):
        pytest.skip(
            f"DB fixtures not found at {DB_FIXTURES_DIR} "
            "(need users.tsv and jobs.tsv — see plan)"
        )

    with PostgresContainer('postgres:16-alpine') as pg:
        url = pg.get_connection_url()  # postgresql+psycopg2://...
        # psycopg2 wants the plain libpq URL.
        libpq_url = url.replace('postgresql+psycopg2://', 'postgresql://')

        with psycopg2.connect(libpq_url) as conn:
            with conn.cursor() as cur:
                cur.execute(SCHEMA_SQL)
                with users_tsv.open() as f:
                    cur.copy_expert(
                        "COPY galaxy_user (id, email) "
                        "FROM STDIN WITH (FORMAT csv, HEADER, "
                        "DELIMITER E'\\t')",
                        f,
                    )
                with jobs_tsv.open() as f:
                    cur.copy_expert(
                        "COPY job (id, tool_id, create_time, user_id) "
                        "FROM STDIN WITH (FORMAT csv, HEADER, "
                        "DELIMITER E'\\t')",
                        f,
                    )
                # Add one anonymous-user job 1 s after a real abricate
                # submission timestamp so the per-test synthetic record
                # can land within the ±5 s match window.
                cur.execute(
                    "INSERT INTO job (id, tool_id, create_time, user_id) "
                    "VALUES (%s, %s, %s, NULL)",
                    (
                        999999999,
                        'toolshed.g2.bx.psu.edu/repos/iuc/abricate/'
                        'abricate/1.4.0',
                        '2026-06-25 23:59:30',
                    ),
                )
            conn.commit()

        yield url


@pytest.fixture()
def db_env(pg_url, monkeypatch):
    """Make the runner connect to the testcontainer."""
    monkeypatch.setenv('GALAXY_DATABASE_URL', pg_url)
    yield pg_url


@pytest.fixture()
def db_conn(pg_url):
    """Direct SQLAlchemy connection for tests that need to query directly."""
    engine = create_engine(pg_url)
    with engine.connect() as conn:
        yield conn
    engine.dispose()


@pytest.fixture(scope='session')
def s3_records():
    """One-time fetch of all records for PINNED_DATE; list of (key, records).

    Tests stream from this snapshot via FakeS3 rather than re-hitting S3.
    Skips the whole integration suite if cleanup has swept the day.
    """
    from common.s3 import S3Storage
    s3 = S3Storage(prefix=os.environ['S3_PREFIX_TOOL_RUNS'])
    snapshot = []
    for key in s3.iter_keys(PINNED_DATE, PINNED_DATE):
        snapshot.append((key, list(s3.read_records(key))))
    if not snapshot:
        pytest.skip(
            f"No S3 keys for {PINNED_DATE} under "
            f"{os.environ['S3_PREFIX_TOOL_RUNS']}"
        )
    return snapshot


class FakeS3:
    """Replays a pre-captured (key, records) snapshot. Mutable for tests
    that need to inject synthetic records."""

    def __init__(self, snapshot, prefix):
        # deep-ish copy — records inside a key list can be appended to.
        self._kr = [(k, list(rs)) for k, rs in snapshot]
        self.prefix = prefix

    def iter_keys(self, start, end):
        for k, _ in self._kr:
            yield k

    def read_records(self, key):
        for k, rs in self._kr:
            if k == key:
                yield from rs

    def date_from_key(self, key):
        # Same algorithm as S3Storage.date_from_key.
        date_str = key[len(self.prefix):len(self.prefix) + 10]
        return date.fromisoformat(date_str.replace('/', '-'))

    def append_record(self, record):
        """Inject a synthetic record into the first key."""
        if not self._kr:
            raise RuntimeError("No keys to append to")
        self._kr[0][1].append(record)


@pytest.fixture()
def fake_s3(s3_records, monkeypatch):
    """Patch runner.S3Storage so runner.run() reads from the snapshot."""
    fake = FakeS3(s3_records, prefix=os.environ['S3_PREFIX_TOOL_RUNS'])
    monkeypatch.setattr(
        'common.runner.S3Storage', lambda prefix=None: fake)
    yield fake


@pytest.fixture()
def captured_writes(monkeypatch):
    """Replace write_to_influxdb with a list-append; yield the list."""
    captured = []
    monkeypatch.setattr(
        'common.runner.write_to_influxdb',
        lambda lines: captured.extend(lines),
    )
    return captured


@pytest.fixture()
def temp_state_dir(tmp_path, monkeypatch):
    """Per-test state dir, swapped into reports.tools.REPORT."""
    from reports import tools as tools_report
    state_dir = tmp_path / 'state'
    state_dir.mkdir()
    monkeypatch.setattr(tools_report.REPORT, 'state_dir', state_dir)
    yield state_dir
    if state_dir.exists():
        shutil.rmtree(state_dir, ignore_errors=True)
