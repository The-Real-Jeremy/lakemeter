# Changes in this fork

This repository is a modified copy of [databrickslabs/lakemeter-oss](https://github.com/databrickslabs/lakemeter-oss)
(upstream commit `426cb36`, v0.2.3), distributed under the upstream Databricks License
([LICENSE.md](LICENSE.md)) with its notices kept ([NOTICE.md](NOTICE.md)). Every modified
upstream source file starts with a line saying so. This file lists them all.

## Modified upstream files

| File | Change |
| --- | --- |
| `README.md` | Fork sections (Install, deploy, prices, tests) first; the upstream README follows |
| `backend/app/bootstrap/runner.py` | SQLite bootstrap; loads `backend/prices` on top of the bundled snapshot (both backends) |
| `backend/app/database.py` | `DB_BACKEND=sqlite` engine |
| `backend/app/main.py` | SQLite start-up, snapshot after every write, `/api/v1/system/features`, `/persistence`, `/prices` |
| `backend/app/models/*.py` (7 files) and `models/__init__.py` | Portable column types; PostgreSQL column semantics on SQLite |
| `backend/app/routes/chat.py` | `503 AI_ASSISTANT_DISABLED` when no model serving endpoint is available |
| `backend/app/routes/reference/vector_search.py` | `CAST(... AS BIGINT)` instead of `::bigint` |
| `backend/app/services/lakebase_queries.py` | Pricing functions run in Python on SQLite |
| `backend/app.yaml` | Deploys the SQLite backend; upstream's Lakebase version is kept as `backend/app.lakebase.yaml` |
| `backend/requirements.txt` | Offline install from `backend/wheels`; upstream's list is now `backend/requirements-postgres.txt` |
| `backend/static/index.html`, `backend/static/assets/*` | Frontend rebuilt from the modified sources below |
| `frontend/src/api/client.ts`, `frontend/src/components/Layout.tsx` | Feature, storage and price status in the UI |
| `tests/test_installation/test_marketplace_package.py` | Reads the preserved Lakebase config files |
| `.gitignore` | Ignores `dist/` |

## Added

`DEPLOY.py` (browser-only install notebook), `backend/app/sqlite_backend.py`, `backend/app/persistence.py`, `backend/app/features.py`,
`backend/app/prices_info.py`, `backend/app/models/types.py`, `backend/app/services/pg_numeric.py`,
`backend/app/services/pricing_engine.py`, `backend/prices/`, `backend/wheels/` (unmodified
third-party wheels under their own licences), `catalog.yaml`, `scripts/refresh_azure_prices.py`,
`scripts/vendor_wheels.sh`, `scripts/build_airgap_zip.sh`, `tests/backend_parity/`,
`tests/sqlite_backend/`, `docs/SQLITE_BACKEND.md`, this file.

## Removed from this fork

Upstream files that target upstream's own development workspace (they embed its host and
staff e-mail addresses and need that workspace to run). They remain available upstream:
`etl/lakebase_setup/{debug,documentation,release_2,release_3,setup,tests}/` (the function
sources in `etl/lakebase_setup/functions/` are kept), `tests/ai_assistant/`, `tests/e2e/`,
`tests/harness/`, `tests/test_lakebase_permissions.py`, the two meta-tests that check those suites exist (`tests/test_integration_validation/test_suite_completeness.py`,
`test_workload_coverage.py`),
`backend/.databricks/` (a CLI sync snapshot), `blog-drafts/`, and the documentation site
`docs-site/` with its tests `tests/docs_media/` (its videos and screenshots would push the repo ZIP
over the 10 MB workspace-import limit; the documentation is online at
https://databrickslabs.github.io/lakemeter-oss/ and the README shows upstream's images from upstream).

## Placeholders and test adjustments in this copy

Neutral placeholders replace upstream's hosts and e-mail addresses in:
`deploy.sh` (host), `scripts/populate_all_workloads.py` (host), `backend/app/external_api.py`
(an unused constant, now `LAKEMETER_EXTERNAL_API_BASE`), `backend/app/routes/estimates.py` (an
e-mail in a comment), the four e-mail fixtures in `tests/export/all_purpose/test_allpurpose_export_integration.py`,
`tests/export/dbsql/test_dbsql_export_integration.py`, `tests/export/dbsql/test_dbsql_integration_multi.py`
and `tests/export/jobs/test_jobs_export_integration.py`, and the `LOCAL_DEV_EMAIL` hint in
`frontend/src/components/Layout.tsx`. `tests/export/cross_workload/test_regression_s10.py` skips its
check of the removed `tests/ai_assistant/`; `scripts/update_version.py` and `scripts/validate_release.py`
no longer list `docs-site/` version files. The lockfile `frontend/package-lock.json` resolves from the
public npm registry.
