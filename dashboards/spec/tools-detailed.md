# `dashboards/tools-detailed.json` — Galaxy tools — Detailed

Grafana dashboard (schemaVersion 39) over the InfluxDB 1.x `tool_runs` measurement.
Lets a user search for a family of tools by name and see aggregate and per-tool usage
for that family. Companion to `tools-overview.json`, which shows top-N summaries with
no search.

## Data model

Measurement `tool_runs`, one point per tool-run log record (see `reports/tools.py`):

- Tags: `domain`, `tool_id`, `tool_version`, `institution` (empty string if the run
  couldn't be joined to a Galaxy `job` row — see enrichment note below).
- Fields: `count` (always `1.0`), `user_id` (only present when the job join succeeded).

## Template variables

Evaluated in this order because each one's query references the ones before it:

1. **`domain`** (query, single-select, `includeAll`) — every domain with runs in the
   time range. `allValue: ".*"` so the regex guard `"domain" =~ /^${domain:regex}$/`
   matches everything when `All` is selected.
2. **`institution`** (query, single-select, `includeAll`) — institutions with runs
   matching the current `domain` **and** `tool_search`, in the current time range.
   Uses `allValue: "'__all__'"`, a literal sentinel rather than a regex, because
   `institution` can be an empty string on unenriched rows and a wildcard regex would
   still filter differently than "no filter" — every query guards it explicitly:
   `(${institution:singlequote} = '__all__' OR "institution" =~ /^${institution:regex}$/)`.
3. **`tool_search`** (textbox, default `""`) — free text, contains-match against
   `tool_id`. Every query guards it the same way:
   `(${tool_search:singlequote} = '' OR "tool_id" =~ /${tool_search:regex}/)`.
   The `:regex` formatter escapes metacharacters, so this is always a literal substring
   match, never a user-supplied regex — a stray `(` or `*` cannot break a query.

`institution` has `refresh: 2` (re-run on time range change), and Grafana re-runs a
query variable automatically whenever any variable it references changes — so editing
`tool_search` cascades into it without extra configuration.

**Why `institution` also filters by `tool_search`:** without it, searching a niche
tool would still offer every institution in the whole dataset in the dropdown, most of
which have zero runs for that search. Scoping the variable's own query keeps the
dropdown honest. The same guard clause used in queries (`${tool_search:singlequote} = ''
OR ...`) is reused here so blank search still returns the full unscoped list.

## Panels

Row 0 — stat summary row (side by side, `y: 0`, `h: 3`, `w: 8` each):

- **id 12 — "Number of tools matched"**. Same query as id 10 (`GROUP BY "tool_id"`),
  reduced to a single count via `seriesToRows` → `organize` (drop `Metric`) → `reduce`
  (`count`). The value is the number of distinct matching tools, not a run total.
- **id 13 — "Number of institutions matched"**. Same query as id 11 (`GROUP BY
  "institution"`, excludes unenriched empty-institution runs), reduced the same way as
  id 12.
- **id 14 — "Total number of jobs"**. `sum("count")` with no `GROUP BY`, so InfluxDB
  already returns one point — the stat panel's own `reduceOptions.calcs: ["sum"]`
  reads it directly, no transformation needed.

Row 1 (side by side, `y: 3`, `h: 9` each):

- **id 10 — "Matching tools by runs"** (table, `w: 12`, `x: 0`). Every tool matching
  `tool_search`, ranked by total runs, filtered by `domain`/`institution`, limited to 50.
- **id 11 — "Matching tools by institution"** (table, `w: 12`, `x: 12`). Same filter
  set, `GROUP BY "institution"` instead of `tool_id`. Adds `AND "institution" != ''` to
  exclude unenriched runs from the total (see note below) — the tools-by-runs panel
  intentionally does **not** exclude these, since it counts runs regardless of whether
  the institution could be resolved.

Both tables share the same transformation chain: `seriesToRows` → `sortBy` (desc on
`Value`) → `limit` (50) → `organize` (rename `Metric`/`Value` to the panel's row label
and `Runs`, drop `Time`). id 11 was built by deep-copying id 10 and only touching the
query, alias, title, id, `gridPos.x`, and the `Metric` rename — keep them structurally
identical if either is edited again.

Below that, two full-width time series:

- **id 8 — "Runs over time — all tools matching "$tool_search""** (`y: 12`, `h: 9`).
  Single aggregate series, `GROUP BY time($__interval)`.
- **id 9 — "Runs over time — per tool"** (`y: 21`, `h: 12`). One series per matching
  tool (`GROUP BY "tool_id", time($__interval)`). Unlike every other panel, its query
  guard is `${tool_search:singlequote} != '' AND "tool_id" =~ /${tool_search:regex}/`
  (not the usual `= '' OR ...` pass-through) — a blank `tool_search` makes the `WHERE`
  clause false, so the panel renders no series at all rather than one per tool across
  the whole dataset (~800+) on initial load. A `renameByRegex` transformation shortens
  toolshed legend labels from the full repo path to `owner/tool`
  (`.*/repos/([^/]+)/[^/]+/(.*)` → `$1/$2`); tools with no `/repos/` segment (e.g.
  built-in `upload1`) pass through unchanged.

## The empty-`institution` gap (enrichment)

`institution` (and the `user_id` field) come from joining each nginx log record to a
Galaxy `job` row within a ±5 second window (`reports/tools.py::_get_job_for_record`).
If that join fails, both come back empty/absent on the point, but `count` is still
written — so run totals stay accurate even when enrichment breaks, but institution
breakdowns silently lose coverage.

This happened in practice: 2026-07-21 to 2026-09-03, the reporting service's DB
connection pointed at a stale backup rather than the live Galaxy Postgres instance, so
every job join for that window failed and `tools-detailed`'s institution panel (and
`tools-overview`'s "Top 10 institutions") showed no data for the period. Fixed by
repointing the connection and backfilling. If institution/user breakdowns look empty
or truncated again, check whether the run-count panels for the same range look normal
first — if runs are flowing but institutions/users aren't, suspect the job-join
pipeline before suspecting the dashboard.

## Editing this dashboard

The project convention (see git history) is to script edits rather than hand-edit the
JSON: load with `json.load(..., object_pairs_hook=OrderedDict)`, mutate, write back
with `json.dumps(dashboard, indent=2, ensure_ascii=False) + '\n'`. Always validate
afterwards by reloading the JSON and checking panel `id`s stay unique — Grafana fails
silently (or overwrites one panel with another) on duplicate ids.

When adding a filter clause to one query, check whether the `institution` template
variable needs the same clause added to its own `query`/`definition` field — otherwise
the variable's dropdown options can drift out of sync with what the panels actually
show.
