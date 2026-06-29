# Integration tests

End-to-end tests that drive each report's full pipeline — live S3 → record parse → real Postgres lookup → line-protocol → captured Influx writes — against a Postgres testcontainer seeded from [tests/data/](../data/).

These tests cover the bits unit tests can't reach: real SQL execution against a real driver, real `bytea`/`memoryview` shapes, the live upstream `domains.json` lookup, the Blowfish-based Galaxy ID encoding, and the exact line-protocol output emitted by `build_points`.

## Running

```bash
# unit tests only (integration deselected)
./venv/bin/python -m pytest

# integration suite (requires Docker + a populated .env)
RUN_INTEGRATION=1 ./venv/bin/python -m pytest -m integration -v

# both
RUN_INTEGRATION=1 ./venv/bin/python -m pytest
```

Gating happens in [tests/conftest.py](../conftest.py) via `pytest_collection_modifyitems`. The integration marker is registered in [pytest.ini](../../pytest.ini).

### Prerequisites

- Docker daemon reachable (testcontainers boots `postgres:16-alpine`).
- A `.env` at the repo root with valid `S3_*` credentials for the live bucket, plus `GALAXY_ID_SECRET` (workflow tests skip without it).
- DB fixture TSVs in [tests/data/](../data/) — see "Fixture data" below.

## Layout

```
tests/integration/
  conftest.py                  # Postgres testcontainer + fixtures
  fixtures/
    schema.sql                 # minimal Galaxy schema (only columns the queries read)
  test_tools_pipeline.py       # `tools` report
  test_workflows_pipeline.py   # `workflows` report
```

## Fixtures

Defined in [conftest.py](conftest.py):

- `pg_url` (session) — boots the testcontainer, applies `schema.sql`, COPYs `users.tsv`, `jobs.tsv`, and (if present) `workflows.tsv` + `stored_workflows.tsv`. Yields a SQLAlchemy URL. Also inserts one synthetic anonymous-user job at `2026-06-25 23:59:30` for the ±5 s window test.
- `db_env` — monkeypatches `GALAXY_DATABASE_URL` so `runner.run` connects to the testcontainer.
- `db_conn` — a direct SQLAlchemy connection for tests that need to query directly.
- `s3_records` / `s3_workflow_records` (session) — one-time S3 fetch for `PINNED_DATE` (2026-06-25) under the tool / workflow prefix. `skipIf` when empty (cleanup ran) or, for workflows, when `GALAXY_ID_SECRET` is unset.
- `FakeS3` + `fake_s3` / `fake_s3_workflows` — replay the captured snapshot via the `S3Storage` interface. Tests can `append_record(...)` to inject synthetic records (used for boundary / error-path coverage).
- `captured_writes` — monkeypatches `common.runner.write_to_influxdb` to append to a list. Tests assert against this list.
- `temp_state_dir` / `temp_state_dir_workflows` — per-test `STATE_DIR` under `tmp_path`. Built via the shared `_make_temp_state_dir` helper. The yielded path is the report's subdir (`<root>/<report.name>`); the per-month state files appear under it after a successful run.

`PINNED_DATE = date(2026, 6, 25)` is defined in conftest. Both suites pin to the same day; the workflow fixture data is exported from invocations on that date.

## Fixture data

All fixtures live in [tests/data/](../data/) (gitignored where they contain user-derived content — see project-level rules). They are loaded into the testcontainer by `pg_url`.

| File | Columns | Loaded into |
|---|---|---|
| `users.tsv` | `id, email` | `galaxy_user` |
| `jobs.tsv` | `id, tool_id, create_time, user_id` | `job` |
| `stored_workflows.tsv` | `id, name, user_id, latest_workflow_id` | `stored_workflow` |
| `workflows.tsv` | `id, uuid, source_metadata` | `workflow` (bytea round-trips via `\x<hex>`) |

Emails in `users.tsv` are scrambled to an `[a-f0-9]{8}@<domain>` pattern. The domain is preserved so the upstream `domains.json` institution lookup still resolves correctly.

### Regenerating from production

The TSVs are exports from a `psql` session connected to the Galaxy DB, pinned to `2026-06-25`. Schematically, each export is a `\copy (SELECT ...) TO 'tests/data/<name>.tsv' WITH (FORMAT csv, HEADER, DELIMITER E'\t')`:

- `jobs.tsv` — `job` rows for the pinned day with tool ids filtered to whatever the matching nginx fixtures reference.
- `users.tsv` — distinct `galaxy_user` rows referenced by any seeded `job.user_id` or `stored_workflow.user_id`. Scramble emails locally to the `[a-f0-9]{8}@<domain>` pattern after export.
- `stored_workflows.tsv` — `stored_workflow` rows whose `latest_workflow_id` has any `workflow_invocation` on the pinned day.
- `workflows.tsv` — the corresponding `workflow` rows. `source_metadata` is `bytea` in production; the default `bytea_output = hex` setting makes `\copy` emit `\x<hex>` literals that round-trip straight back into a `bytea` column via the test schema.

## Adding tests for a new report

1. Extend [fixtures/schema.sql](fixtures/schema.sql) with the minimal table(s) the report's SQL reads.
2. Add fixture TSV(s) under [tests/data/](../data/), and extend `pg_url` in `conftest.py` to COPY them (FK-ordered; conditional on file existence).
3. Add a session-scoped `s3_<report>_records` fixture and a `fake_s3_<report>` fixture (mirror the workflow ones).
4. Add a `temp_state_dir_<report>` fixture (delegate to `_make_temp_state_dir`).
5. Create `test_<report>_pipeline.py`, mark every test `@pytest.mark.integration`, and run `runner.run(<report>.REPORT, PINNED_DATE, PINNED_DATE, dry=False)` per test.

## Notes

- The `_tags` / `_fields` helpers in `test_workflows_pipeline.py` use a backslash-escape-aware splitter (`_split_unescaped`) because workflow names and institutions often contain spaces. The tools test's simpler helper splits on raw spaces and only works as long as no tag value contains one.
- `runner.run` only calls `state.mark_ingested` after a non-empty write — keys whose snapshot contributes zero data points are intentionally not marked, so `test_state_marks_key_after_successful_write` uses `issubset` rather than equality.
- Tests do not mock `load_domain_map`; it fetches live from upstream GitHub. Institution assertions therefore depend on the upstream `domains.json` mapping being stable for the chosen domain.
