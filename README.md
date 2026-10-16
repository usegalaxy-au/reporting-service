# Galaxy reporting service

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
download.py               # bulk-downloads a date range of S3 logs
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

## Downloading logs

`download.py` pulls a date range of Nginx logs out of S3 for offline
analysis:

```bash
./venv/bin/python download.py \
    --from 2026-09-01 --to 2026-10-15 \
    --outdir 6weeks --decompress --squash-days 7
```

| Flag            | Description                                              |
| --------------- | -------------------------------------------------------- |
| `--from`        | First day, `YYYY-MM-DD` (required)                       |
| `--to`          | Last day, inclusive (required)                           |
| `--outdir`      | Output directory, created if missing (required)          |
| `--type`        | `cleaned` (default), `raw`, `error`, `tool_runs`, `workflow_invocations` |
| `--server`      | `prod` (default, `galaxy/` in the bucket) or `dev`        |
| `--decompress`  | Gunzip logs before writing (`.log` rather than `.log.gz`) |
| `--squash-days` | Concatenate logs into batches of N days                  |

Objects are read from `<server>/nginx-logs/<type>/YYYY/MM/DD/`. Without
`--squash-days`, each S3 object is saved as `YYYY-MM-DD_<name>` and files
that already exist are skipped, so an interrupted run can be resumed. With
it, batches start at `--from` and are named `<first-day>_<last-day>.log`
(the last batch is clipped to `--to`; days with no logs produce no file).
Batches are rebuilt on every run. Without `--decompress`, a batch is a
series of concatenated gzip members, which `zcat` reads as one stream.

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
