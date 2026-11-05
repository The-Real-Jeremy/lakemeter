# The SQLite backend

For workspaces that have no Lakebase (and often no internet and no Foundation Model APIs).
PostgreSQL remains supported and is unchanged in behaviour.

## How it works

* **Database.** With `DB_BACKEND=sqlite`, every connection ATTACHes the SQLite file as
  `lakemeter`, so all `lakemeter.<table>` SQL and the ORM run unchanged. Bootstrap uses the same
  `schema.sql` (translated to SQLite DDL), `seeds.json` and pricing CSVs; the CSVs load by
  position, exactly as PostgreSQL `COPY` does.
* **Pricing functions.** The 15 PL/pgSQL functions in `functions.sql` are replaced on SQLite by
  `services/pricing_engine.py`, a statement-by-statement port. `services/pg_numeric.py`
  reproduces PostgreSQL NUMERIC arithmetic:
  * float to numeric keeps 15 significant digits;
  * division uses PostgreSQL's own scale rule;
  * variables round when assigned to `DECIMAL(p,s)`, half away from zero, but declared return types do not;
  * SQL NULL logic applies;
  * `CAST(float AS INT)` rounds half to even.
* **Column semantics.** Some ORM types differ from the real columns (for example
  `fmapi_quantity` is `Numeric` in the model and `BIGINT` in the table). On SQLite every numeric
  ORM column rounds on write and returns values the way its PostgreSQL column would. The types are
  read from `schema.sql`, so the two cannot drift apart.
* **Persistence** (`app/persistence.py`). The Databricks Apps filesystem is wiped on every restart
  and redeploy.
  * After any request that writes, the user tables are copied to `LAKEMETER_SNAPSHOT_PATH` before
    the response is returned. This can be a UC volume, a `/Workspace/...` file or `file://`.
  * The snapshot is restored on start. Pricing is rebuilt from the bundled files, so a snapshot is
    small (tens of KB for a few estimates).
  * If a snapshot exists but cannot be restored, uploads are blocked so that an empty session can
    never overwrite good data.
  * The UI shows a red banner when a restore or a save fails, and the footer shows the last save time.
* **AI assistant** (`app/features.py`). `LAKEMETER_AI_ASSISTANT=auto|on|off`. With `auto` (the
  default) it is enabled only if the configured serving endpoint exists and is READY. Otherwise
  the `/chat` routes answer `503 AI_ASSISTANT_DISABLED` and the UI does not render the assistant.

## Parity with PostgreSQL

`tests/backend_parity/run_with_throwaway_postgres.sh` starts a disposable PostgreSQL 16 and
compares the two backends at three levels (see its README):

| Layer | What is compared | Size |
| --- | --- | --- |
| Data | Every table the bootstrap fills, row by row in insertion order | 16 tables |
| Engine | PL/pgSQL `calculate_line_item_costs` vs the Python engine; all 12 outputs compared as strings, and errors must match too | about 3,000 generated calls |
| API | Two live servers: every calculate endpoint over valid and invalid payloads, every reference endpoint, and an estimate with one line item per workload type read back as JSON, Excel cells, after edit and after duplicate | about 690 requests |

The result is 77/77 with both C and en_US.UTF-8 collation. There are two deliberate differences:
* **Sort order.** Text is sorted by byte order on SQLite, so a locale-collated PostgreSQL returns
  some reference lists in a different order. The contents are proved identical.
* **Line item order.** `GET /estimates/{id}` returns its line items in no defined order on either backend.

## Limits

* **One app instance.** The snapshot design assumes one app process, which is what Databricks Apps runs.
* **Save timing.** A save is durable once its request returns.
* **Numeric precision.** NUMERIC columns are stored as SQLite REAL, so they are exact to 15
  significant digits. BIGINT columns are exact.
* **VARCHAR length.** SQLite does not enforce VARCHAR lengths.

## Upstream behaviour the tests surfaced (present on PostgreSQL too; left as is)

* `calculate_fmapi_*_dbu` always fails: `COALESCE(input_divisor, 1)` mixes TEXT and INTEGER. No route calls it.
* `dbsql-warehouse-config.csv` has `warehouse_type` and `warehouse_size` swapped relative to its
  header. As a result, DBSQL Classic/Pro warehouse VM cost is always 0.
* `GET /pricing/dbu-rates?product_type=...` returns 500 when a product type is NULL.
* Timestamps are naive UTC with no `Z`, so the estimate list shows relative times shifted by the
  browser's UTC offset.

## Keeping the fork current

* **Rebasing.** Rebase onto a new upstream tag. Conflicts should be confined to `main.py`,
  `database.py`, `bootstrap/runner.py` and `Layout.tsx`.
* **When upstream changes `functions.sql`,** port the change into `pricing_engine.py` and run the
  parity suite. The engine layer fails digit by digit if the two disagree.
* **When upstream adds a route or a workload type,** add a payload to `tests/backend_parity/test_api_parity.py`.
