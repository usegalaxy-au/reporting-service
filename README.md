# reporting-service

Ingests nginx access logs from S3, enriches them against the Galaxy
database, and writes data points to InfluxDB for dashboarding.

Two reports are wired up today:

| Name        | Source request                          | InfluxDB measurement   |
| ----------- | --------------------------------------- | ---------------------- |
| `tools`     | `POST /api/tools(/fetch)?`              | `tool_runs`            |
| `workflows` | `POST /api/workflows/<id>/invocations`  | `workflow_invocation`  |

## Layout

```
report.py                 # unified CLI entrypoint
s3_cleanup.py             # ages out old S3 log objects
common/
  s3.py                   # S3 client (list / read / date-key parsing)
  influx.py               # line protocol + HTTP write
  state.py                # per-report ingested-key state files
  domains.py              # email-domain → institution lookup
  log.py                  # logging + uncaught-exception hook
  runner.py               # generic per-key ingest loop + Report dataclass
reports/
  tools.py                # tool-run report definition
  workflows.py            # workflow-invocation report definition
state/                    # ingested-key state, one subdir per report
  tools/                  # YYYY-MM files, one S3 key per line
  workflows/
tests/                    # unit tests + opt-in integration suite
```

Adding a third report means adding one file in `reports/` and
registering it in `reports/__init__.py`.

## Setup

```bash
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
cp .env.example .env   # then fill in S3 / Influx / DB credentials
```

Required `.env` variables:

- `S3_ACCESS_KEY`, `S3_SECRET_KEY`, `S3_ENDPOINT_URL`, `S3_REGION`,
  `S3_BUCKET`
- `S3_PREFIX_TOOL_RUNS`, `S3_PREFIX_WORKFLOW_INVOCATIONS`
- `GALAXY_DATABASE_URL` (SQLAlchemy URL, Postgres)
- `GALAXY_ID_SECRET` (only for `workflows` — used to decode workflow IDs)
- `INFLUX_URL`, `INFLUX_DB`, `INFLUX_TOKEN`

## Running

```bash
# default window: last 7 days through today, inclusive
./venv/bin/python report.py tools
./venv/bin/python report.py workflows

# pinned window
./venv/bin/python report.py tools --start 2026-06-25 --end 2026-06-26

# print line protocol to stdout instead of writing to InfluxDB
./venv/bin/python report.py tools --dry
```

Cron entries:

```cron
*/15 * * * * cd /opt/reporting-service && ./venv/bin/python report.py tools
*/15 * * * * cd /opt/reporting-service && ./venv/bin/python report.py workflows
```

State files under `state/<report>/` dedup S3 keys
that have already been ingested, so re-running over the same window is
cheap and idempotent.

## Tests

```bash
# unit tests (fast, no external deps)
./venv/bin/pip install -r requirements-dev.txt
./venv/bin/python -m pytest

# integration tests (Docker + live S3 + GitHub for domains.json)
RUN_INTEGRATION=1 ./venv/bin/python -m pytest
```

The integration suite boots a Postgres testcontainer seeded from
`tests/data/jobs.tsv` + `users.tsv`, pulls real S3 records for a
pinned historical date (cached once per session to avoid repeated
downloads), and asserts on the line-protocol output. See
`tests/integration/conftest.py` for the fixture wiring.
