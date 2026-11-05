# PostgreSQL vs SQLite parity

`DB_BACKEND=sqlite` replaces the PL/pgSQL pricing functions with a Python port
(`backend/app/services/pricing_engine.py`). These tests prove the two backends
give the same answers.

| Layer | What is compared | Size |
|---|---|---|
| Data (`test_data_parity.py`) | every table the bootstrap fills, row by row in insertion order (pricing lookups use `LIMIT 1`, so order matters) | 16 tables |
| Engine (`test_engine_parity.py`) | `lakemeter.calculate_line_item_costs` on PostgreSQL vs `PricingEngine` on SQLite, all 12 outputs compared as strings, errors included | ~3,000 generated calls |
| API (`test_api_parity.py`) | two live servers: every calculate endpoint over a payload grid (valid and invalid), every reference endpoint, and a golden estimate with one line item per workload type read back as JSON, line items, Excel cells, after edit and duplicate | ~690 calculate requests, 40 reference endpoints, 22 line items over all 19 workload types |

Run against a throwaway PostgreSQL (needs `initdb`, `pg_ctl`, `openssl`):

    tests/backend_parity/run_with_throwaway_postgres.sh            # C collation, strict
    tests/backend_parity/run_with_throwaway_postgres.sh --collation en_US.UTF-8

or point `LAKEMETER_PARITY_PG_URL` at any disposable database. Without it the
suite is skipped.

## Known, deliberate differences

* **Text sort order.** SQLite sorts by byte order. PostgreSQL sorts by the
  database collation. With `C` collation every response matches exactly. With
  a locale collation, reference lists (instance types, families) come back in
  a different order; the suite proves the contents are identical.
* **`Estimate.line_items` order.** That relationship has no `ORDER BY`
  upstream, so neither backend promises an order; it is compared by
  `display_order`. Endpoints that do order (`/full`, `/line-items/estimate`)
  are compared as returned.

## Upstream defects these tests surfaced (reproduced on both backends, not fixed)

* `calculate_fmapi_*_dbu` always fails on PostgreSQL:
  `COALESCE(input_divisor, 1)` mixes TEXT and INTEGER. No route calls it.
* `dbsql-warehouse-config.csv` has `warehouse_type` and `warehouse_size`
  swapped relative to its header. `COPY` loads by position, so the DBSQL VM
  cost lookup never matches and Classic/Pro warehouse VM cost is always 0.
* `GET /pricing/dbu-rates?product_type=...` raises `AttributeError` (500)
  because some `product_type` values are NULL.
