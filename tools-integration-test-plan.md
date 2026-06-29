# Refactor: unify tool-run and workflow-invocation collectors

## Context

[tools/tool_runs.py](tools/tool_runs.py) and [workflows/collect_workflow_invocations.py](workflows/collect_workflow_invocations.py) are two cron scripts that follow the same shape:

1. List/iterate S3 keys for a date range (skip already-ingested).
2. For each gzipped JSON nginx log, parse records that match a request pattern.
3. Enrich each match by querying the Galaxy DB (and a domain → institution map).
4. Format an InfluxDB line-protocol point and POST it to the write API.
5. Mark the S3 key ingested.

They share — *as near-verbatim copies* — `load_domain_map`, `lookup_institution`, `escape_tag_value`, `format_line_protocol`, `load_ingested_keys`, `mark_ingested`, `write_to_influxdb`, `parse_args`, logging/excepthook setup, env loading, and the main key-iteration loop. Only the request regex, the DB query, and the tag/field mapping actually differ between them.

Goal: a single cron entrypoint `report.py <report-name> [--start ...] [--end ...] [--dry]` that dispatches to one of several report definitions, with all shared logic in importable modules. Adding a third report (e.g. histories, downloads) should be a single new file.

## Target layout

```
reporting-service/
  report.py                  # NEW — CLI entrypoint + dispatcher
  s3.py                      # unchanged
  s3_cleanup.py              # unchanged
  common/
    __init__.py
    influx.py                # NEW — line protocol + HTTP write
    state.py                 # NEW — load_ingested_keys / mark_ingested
    domains.py               # NEW — load_domain_map / lookup_institution
    log.py                   # NEW — logging + excepthook setup
    runner.py                # NEW — generic per-key ingest loop
  reports/
    __init__.py              # NEW — REGISTRY mapping name -> Report
    tools.py                 # NEW — replaces tools/tool_runs.py
    workflows.py             # NEW — replaces workflows/collect_workflow_invocations.py
    test_workflow_id_mapping.py  # MOVED from workflows/, imports updated
  tools/state/               # kept (state files stay where cron has been writing them)
  workflows/state/           # kept
```

## Shared modules

- [common/influx.py](common/influx.py): `escape_tag_value`, `escape_field_string`, `format_line_protocol`, `write_to_influxdb` (reads `INFLUX_URL`/`INFLUX_DB`/`INFLUX_TOKEN` once at import time, same SSL-relaxed POST). Use the workflow version of `format_line_protocol` since it also escapes field strings — the tool_runs version is a subset.
- [common/state.py](common/state.py): `load_ingested_keys(state_dir, start, end)` and `mark_ingested(state_dir, s3, key)`. Takes `state_dir` as a parameter so each report keeps its own `state/` directory (preserving existing on-disk state — no migration needed).
- [common/domains.py](common/domains.py): `load_domain_map(cache_file)` and `lookup_institution(email, domain_map)`. Cache file path comes from the report's state dir.
- [common/log.py](common/log.py): one-call `setup_logging()` that installs the format and `sys.excepthook`.

## Report definition

Each report is a small dataclass-style object in [reports/](reports/):

```python
@dataclass
class Report:
    name: str                    # CLI arg, e.g. "tools"
    s3_prefix_env: str           # e.g. "S3_PREFIX_TOOL_RUNS"
    measurement: str             # InfluxDB measurement name
    state_dir: Path              # where to persist ingested-key files
    parse_record: Callable[[dict], Optional[dict]]
    build_points: Callable[[Connection, dict, dict], list[str]]
    # build_points takes (db_conn, parsed_record, domain_map) and returns
    # zero or more line-protocol strings. This keeps DB queries and
    # tag/field shaping inside the report module.
```

- `reports/tools.py` holds `TOOL_REQUEST_PATTERN`, `JOB_QUERY`, `JOB_MATCH_WINDOW_SECONDS`, `parse_record`, and `build_points`. Exports `REPORT = Report(name="tools", measurement="tool_runs", s3_prefix_env="S3_PREFIX_TOOL_RUNS", state_dir=Path(__file__).parent.parent / "tools/state", ...)`.
- `reports/workflows.py` holds `INVOCATION_PATTERN`, `WORKFLOW_QUERY`, `decode_galaxy_id`, `resolve_canonical_id`, the Blowfish cipher init, `parse_record`, and `build_points`. Exports `REPORT = Report(name="workflows", measurement="workflow_invocation", s3_prefix_env="S3_PREFIX_WORKFLOW_INVOCATIONS", state_dir=.../"workflows/state", ...)`. The InfluxDB measurement name stays `workflow_invocation` so existing dashboards keep working — only the CLI name changes.
- `reports/__init__.py` exposes `REGISTRY = {r.name: r for r in [tools.REPORT, workflows.REPORT]}`.

## Runner

[common/runner.py](common/runner.py) holds the shared loop, parameterised by a `Report`:

```python
def run(report: Report, start: date, end: date, dry: bool) -> None:
    s3 = S3Storage(prefix=os.environ[report.s3_prefix_env])
    ingested = load_ingested_keys(report.state_dir, start, end)
    domain_map = load_domain_map(report.state_dir / "domains.json")
    engine = create_engine(os.environ["GALAXY_DATABASE_URL"])
    with engine.connect() as conn:
        for key in s3.iter_keys(start, end):
            if key in ingested:
                continue
            points = []
            for record in s3.read_records(key):
                parsed = report.parse_record(record)
                if not parsed:
                    continue
                points.extend(report.build_points(conn, parsed, domain_map))
            if points:
                if dry:
                    for p in points: print(p)
                else:
                    write_to_influxdb(points)
            if not dry:
                mark_ingested(report.state_dir, s3, key)
```

The tool_runs script's per-key summary log line (records / tool matches / job matches / institutions) is genuinely useful — preserve it by having `build_points` return a small counters dict alongside points, or (simpler) move that logging into `build_points` and have it call `logger.info` itself. Pick the simpler option during implementation.

## report.py entrypoint

```python
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("report", choices=list(REGISTRY))
    parser.add_argument("--start", type=date.fromisoformat)
    parser.add_argument("--end", type=date.fromisoformat)
    parser.add_argument("--dry", action="store_true")
    args = parser.parse_args()
    today = date.today()
    start = args.start or today - timedelta(days=LOOKBACK_DAYS)
    end = args.end or today
    if start > end: parser.error(...)
    setup_logging()
    load_dotenv(Path(__file__).parent / ".env")
    run(REGISTRY[args.report], start, end, args.dry)
```

Cron lines become:
```
… python /…/reporting-service/report.py tools
… python /…/reporting-service/report.py workflows
```

## Files to delete / move after migration

- [tools/tool_runs.py](tools/tool_runs.py) — delete; replaced by `reports/tools.py` + `report.py`.
- [workflows/collect_workflow_invocations.py](workflows/collect_workflow_invocations.py) — delete; replaced by `reports/workflows.py` + `report.py`.
- [workflows/test_workflow_id_mapping.py](workflows/test_workflow_id_mapping.py) — move to `reports/test_workflow_id_mapping.py`, update imports to pull `decode_galaxy_id`, `resolve_canonical_id`, `WORKFLOW_QUERY`, the cipher, etc. from `reports.workflows`, and `format_line_protocol` from `common.influx`.

The `tools/state/` and `workflows/state/` directories stay in place so existing dedup state continues to apply.

## Verification

1. `flake8` clean on every new/modified file.
2. `python report.py tools --start 2026-06-20 --end 2026-06-22 --dry` prints line protocol matching what the old script would have produced for the same window (eyeball-diff a few keys against a stashed run of the old script before deletion).
3. Same for `python report.py workflows …`.
4. Run once without `--dry` against a single past day already in the state file (it should be a no-op — no S3 reads, no writes).
5. Run without `--dry` for a fresh day, confirm an InfluxDB query for `tool_runs` / `workflow_invocation` measurements shows the new points and that the corresponding S3 keys appear in `tools/state/YYYY-MM` and `workflows/state/YYYY-MM`.
6. Update the cron entries on the deployment host to call the new `report.py`.

---

# Plan: integration tests for the `tools` report

## Context

The unit suite in [tests/](tests/) covers each common module in isolation but doesn't exercise the wired-up pipeline. The risky part of [reports/tools.py](reports/tools.py) — `JOB_QUERY` matching, the +/- 5 s `create_time` window, the tool_id-vs-tool_id_full split, the institution resolution against a real domain map, and the line-protocol output for an end-to-end record — is invisible to unit tests because they all mock `conn.execute`.

`JOB_QUERY` uses `EXTRACT(EPOCH FROM ...)` which is Postgres-only, so SQLite is not a viable shortcut; a real Postgres is required.

Goal: an opt-in integration test that drives the full `tools` pipeline (S3 fixture → record parse → real Postgres lookup → line-protocol → captured Influx writes) against the recorded nginx + job data in [ignore/tool-db-test/](ignore/tool-db-test/), and asserts on the produced line-protocol output.

## Test data on hand

- [ignore/tool-db-test/nginx-jobs.txt](ignore/tool-db-test/nginx-jobs.txt) — 55 raw nginx access-log lines on 2026-06-25, mix of `POST /api/tools` and `POST /api/tools/fetch`. Two distinct tools appear (`abricate/1.4.0` and `abricate_summary/1.4.0`), all with referer `https://usegalaxy.org.au/?tool_id=...&version=latest`. Useful for: pattern matching, referer-based tool_id recovery, the `/fetch` variant, and (because the jobs dataset has no `abricate_summary` rows) the "no job matched" code path.
- [ignore/tool-db-test/jobs.txt](ignore/tool-db-test/jobs.txt) — 225 rows of `create_time | tool_id` for abricate only. **Missing the columns the integration test needs.**

## Test data to fetch (action for the user)

S3 data is read live by the test — no download/checkin step. The fixture instantiates a real `S3Storage` from the existing `.env`, pinned to a fixed historical date (e.g. `2026-06-25`) so the run is reproducible. Expected counts are derived from the same S3 iteration at test setup time, so the assertions stay self-consistent even as S3 content drifts (as long as [s3_cleanup.py](s3_cleanup.py) hasn't swept the chosen date, which the test should pre-check and `skipIf` when empty).

The DB fixture is the only thing the user needs to pull. The `JOB_QUERY` in [reports/tools.py](reports/tools.py) selects `j.id, j.tool_id, j.create_time, j.user_id, u.email` and matches on `create_time` ± 5 s.

1. **`jobs.tsv`** — widened export of the jobs already in [jobs.txt](ignore/tool-db-test/jobs.txt), plus the matching `abricate_summary` rows that exist in the DB for the same window (so the "matched" path is also exercised for that tool). Columns: `id`, `tool_id`, `create_time`, `user_id`. Run from a `psql` session connected to the Galaxy DB:
   ```
   \copy (
     SELECT id, tool_id, create_time, user_id
     FROM job
     WHERE create_time >= '2026-06-25 00:00:00+00'
       AND create_time <  '2026-06-26 00:00:00+00'
       AND tool_id IN (
         'toolshed.g2.bx.psu.edu/repos/iuc/abricate/abricate/1.4.0',
         'toolshed.g2.bx.psu.edu/repos/iuc/abricate/abricate_summary/1.4.0'
       )
   ) TO 'ignore/tool-db-test/jobs.tsv' WITH (FORMAT csv, HEADER, DELIMITER E'\t')
   ```
2. **`users.tsv`** — `id`, `email` for every distinct `user_id` referenced in `jobs.tsv`. The real `users.tsv` will inherit whatever institutions the real users have — that's good enough to exercise the domain-lookup path (one row mapping to a known institution is all the test needs to assert against). NULL `user_id` jobs already cover the anonymous-submission `LEFT JOIN` case. Run from the same `psql` session:
   ```
   \copy (
     SELECT DISTINCT u.id, u.email
     FROM galaxy_user u
     JOIN job j ON j.user_id = u.id
     WHERE j.create_time >= '2026-06-25 00:00:00+00'
       AND j.create_time <  '2026-06-26 00:00:00+00'
       AND j.tool_id IN (
         'toolshed.g2.bx.psu.edu/repos/iuc/abricate/abricate/1.4.0',
         'toolshed.g2.bx.psu.edu/repos/iuc/abricate/abricate_summary/1.4.0'
       )
   ) TO 'ignore/tool-db-test/users.tsv' WITH (FORMAT csv, HEADER, DELIMITER E'\t')
   ```
   If the resulting set is dominated by a single domain, hand-edit a few rows in `users.tsv` to swap in the missing categories (one exact-domain hit, one wildcard hit, one unmapped foreign domain). Real DB ids should be preserved so the FK from `jobs.tsv` still resolves.

Place these alongside the existing data: `ignore/tool-db-test/jobs.tsv` and `ignore/tool-db-test/users.tsv`. They stay in `ignore/` (gitignored) since they contain user emails.

## Layout

```
tests/
  test_influx.py            # existing unit tests
  test_state.py
  test_domains.py
  test_log.py
  test_runner.py
  integration/
    __init__.py
    conftest.py             # NEW — Postgres testcontainer + schema/seed fixture
    fixtures/
      schema.sql            # NEW — minimal job + galaxy_user CREATE TABLE
                            #       statements (only the columns JOB_QUERY reads)
    test_tools_pipeline.py  # NEW — the integration tests
```

`ignore/tool-db-test/` stays the source of truth for the raw fixtures; `tests/integration/fixtures/` only holds derived/static helpers.

## Approach

### 1. Postgres fixture (`tests/integration/conftest.py`)

- `pytest` + `testcontainers[postgres]` (add to [requirements.txt](requirements.txt) as a dev dep, or a separate `requirements-dev.txt`).
- Session-scoped fixture: start a Postgres container, apply [schema.sql](tests/integration/fixtures/schema.sql), `COPY` `users.tsv` then `jobs.tsv`, yield a `GALAXY_DATABASE_URL`-shaped string.
- Function-scoped fixture: a fresh psycopg2/SQLAlchemy connection on top, monkeypatched into `os.environ['GALAXY_DATABASE_URL']`.
- Switch the suite to `pytest` (already implied by testcontainers) — `pytest` discovers `unittest.TestCase` classes, so the existing unit tests keep working without rewrites. Add a top-level `pytest.ini` (or `pyproject.toml [tool.pytest.ini_options]`) configuring `testpaths = tests` and an `integration` marker that's deselected unless `RUN_INTEGRATION=1`.

### 2. Real S3 access

- The test uses `S3Storage` for real, reading from the live bucket configured in `.env`. The test pins to a fixed historical date (e.g. `date(2026, 6, 25)`) so the run is reproducible. A session-scoped fixture lists keys for that date once; if zero keys are returned (cleanup ran, prefix changed) all integration tests `skipIf` with a clear message. The temp `state_dir` is empty per test, so dedup never short-circuits the run.

### 3. Captured Influx writer

- Monkeypatch `common.runner.write_to_influxdb` to append to a list; assert against the list. (Already proven in [tests/test_runner.py](tests/test_runner.py).)

### 4. Domain map

- Let `load_domain_map` run for real against the temp `state_dir` so it fetches the live upstream `domains.json` from GitHub (same source the production script uses). The assertion in `test_matched_job_resolves_user_and_institution` picks an email whose institution mapping is stable upstream (e.g. an `@uq.edu.au` row) rather than hard-coding the expected institution string per fixture.

### 5. The actual tests (`test_tools_pipeline.py`)

All decorated with the `integration` marker. Each runs `common.runner.run(reports.tools.REPORT, start, end, dry=False)` after the fixtures wire everything up, then asserts on the captured line-protocol list:

- **`test_all_requests_emit_one_point_each`** — number of captured lines equals the count of `POST /api/tools(?:/fetch)?` records derived from a separate pass over the same S3 objects in setup. Self-consistent — proves the regex (including `/fetch`) and timestamp parser handle everything S3 returns, without hard-coding a number that drifts with cleanup.
- **`test_tool_id_and_version_split`** — every line has tag `tool_id=...abricate/abricate` (or `.../abricate_summary`) and `tool_version=1.4.0`. Confirms the `rpartition('/')` split.
- **`test_domain_tag_from_referer`** — every line has tag `domain=usegalaxy.org.au`.
- **`test_matched_job_resolves_user_and_institution`** — for a known abricate request whose timestamp lands within ±5 s of a seeded job row, the line has `user_id=...i` field and the expected `institution=...` tag.
- **`test_unmatched_job_emits_point_with_no_institution_or_user`** — the `abricate_summary` requests still emit a line-protocol point (count=1.0) but with no `institution` tag (omitted because empty) and no `user_id` field. Confirms the LEFT-JOIN-style "no job found" fallback.
- **`test_anonymous_user_omits_user_id_field`** — a request matching a job row whose `user_id` is NULL emits a point without a `user_id` field.
- **`test_state_marks_key_after_successful_write`** — after the run, `tools/state/2026-06` (under the temp state dir) contains the fake S3 key.
- **`test_rerun_skips_already_ingested_key`** — call `run()` twice; second call produces zero captured writes.
- **`test_job_window_boundary`** — feed one synthetic JSON record (constructed in-process, appended to the fixture stream) timestamped 6 s away from any seeded job row; assert the resulting line has no `user_id` field, proving the `JOB_MATCH_WINDOW_SECONDS = 5` boundary.

### 6. Running

```bash
# unit tests only (default)
./venv/bin/python -m pytest

# unit + integration (requires Docker)
RUN_INTEGRATION=1 ./venv/bin/python -m pytest -m integration
# or both:
RUN_INTEGRATION=1 ./venv/bin/python -m pytest
```

## Files to create

- [tests/integration/__init__.py](tests/integration/__init__.py)
- [tests/integration/conftest.py](tests/integration/conftest.py)
- [tests/integration/fixtures/nginx_to_json.py](tests/integration/fixtures/nginx_to_json.py)
- [tests/integration/fixtures/domains.json](tests/integration/fixtures/domains.json)
- [tests/integration/fixtures/schema.sql](tests/integration/fixtures/schema.sql)
- [tests/integration/test_tools_pipeline.py](tests/integration/test_tools_pipeline.py)
- [pytest.ini](pytest.ini) — `testpaths`, `markers = integration`, optional `addopts = -m "not integration"` unless `RUN_INTEGRATION` is set (handled via a `conftest.py` `pytest_collection_modifyitems` hook)
- [requirements-dev.txt](requirements-dev.txt) — `pytest`, `testcontainers[postgres]`, `flake8`

## Verification

1. `./venv/bin/python -m pytest` runs only the unit suite (44 tests today, still passes); integration tests appear as `deselected`.
2. `RUN_INTEGRATION=1 ./venv/bin/python -m pytest -m integration -v` boots Postgres, seeds data, and all integration tests pass.
3. `flake8` clean on every new file.
4. Sanity: the count assertion in `test_all_requests_emit_one_point_each` should equal `grep -cE 'POST /api/tools(/fetch)? ' ignore/tool-db-test/nginx-jobs.txt`.

