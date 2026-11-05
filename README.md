# Lakemeter (SQLite fork)

## Install (browser only: no terminal, no CLI, works air-gapped)

Everything the app needs is in this repo: the wheels it installs from, the prebuilt frontend and the prices.
Two ways to install it; both use the `DEPLOY` notebook.

### A) Install on its own (a standalone Databricks App)
1. On this GitHub page: **Code → Download ZIP** (`lakemeter-main.zip`). No need to unzip it.
2. In Databricks: **Workspace** → a folder (e.g. your home) → **⋮ → Import** → drop the `.zip` → **Import**.
   Databricks unzips it into `lakemeter-main/`.
3. Open **`lakemeter-main/DEPLOY`**, attach **Serverless**, set **mode = `standalone`** and
   **snapshot_volume** to a UC volume for the estimates (`<catalog>.<schema>.<volume>`; it is created if
   missing), then **Run all**. If your workspace requires a budget policy, it picks the only one in use, or
   asks you to paste one into **budget_policy_id**.
4. The last cell prints the **app URL** and the app's health (SQLite, saved, prices as of).

### B) Install as a child of the [App Catalog](https://github.com/The-Real-Jeremy/app-catalog)
1. Install the App Catalog first (its README, same four steps), so `app-catalog-main/` is in your workspace.
2. On this GitHub page: **Code → Download ZIP** (`lakemeter-main.zip`).
3. In Databricks: open **the same folder that holds `app-catalog-main/`** → **⋮ → Import** → drop the `.zip`.
   It unzips into `lakemeter-main/` next to the catalog.
4. Give the **catalog app** the volume that keeps the estimates: Compute → Apps → the catalog app → Edit →
   App resources → **+ Add resource → UC volume**, permission **Can read and write**, key
   **`lakemeter--snapshot-volume`**. (Without it the app runs but shows a red banner: estimates are not kept.)
5. Open **`lakemeter-main/DEPLOY`**, attach **Serverless**, leave **mode = `child`** (`catalog_folder` is
   filled in for you), then **Run all**. It copies the app into the catalog's `child_apps/lakemeter/` and
   redeploys the catalog; it does not create or change any app. The last cell prints
   `<catalog URL>/apps/lakemeter/` and checks the volume from step 4.

To update either way: first **rename or delete the old `lakemeter-main/` folder** (an import onto an
existing folder is refused with `RESOURCE_ALREADY_EXISTS` and the old folder stays), then import the new ZIP and
**Run all** `DEPLOY`. After re-importing the App Catalog itself, re-run each child's `DEPLOY` in child mode.
In child mode `DEPLOY` keeps the previous child copy as `.bak-child-lakemeter-<time>`, never deletes it.

---

> **This is a fork** of [databrickslabs/lakemeter-oss](https://github.com/databrickslabs/lakemeter-oss)
> (upstream v0.2.3, commit `426cb36`) that adds a **SQLite backend**, so Lakemeter runs as a
> Databricks App in a workspace with **no Lakebase**, **no internet** and **no Foundation Model APIs**.
> It is not an official Databricks Labs release. It is distributed under the upstream
> [Databricks License](LICENSE.md), with the upstream [NOTICE](NOTICE.md) kept; every changed file
> is listed in [FORK_CHANGES.md](FORK_CHANGES.md). The upstream README follows the fork sections below.

## What the fork adds

* **SQLite instead of PostgreSQL**, selected with `DB_BACKEND=sqlite`; PostgreSQL still works.
  The PL/pgSQL pricing functions are ported to Python and proved digit for digit against
  PostgreSQL 16. See [docs/SQLITE_BACKEND.md](docs/SQLITE_BACKEND.md) and
  [tests/backend_parity](tests/backend_parity/README.md).
* **Estimates survive restarts.** After every save the user data is copied to a UC volume, and it
  is restored on start. If the copy cannot be restored, the app refuses to overwrite it and shows a
  red banner.
* **Offline install.** `backend/requirements.txt` is `--no-index --find-links wheels`. The packages
  the Databricks Apps runtime does not pre-install (SQLAlchemy, pydantic-settings, XlsxWriter, the
  Databricks SDK and a few small dependencies; 14 pure-Python wheels) are committed in `backend/wheels`;
  FastAPI, Uvicorn, pydantic and the rest come from the runtime. The frontend is prebuilt in `backend/static`.
* **AI assistant off when there is no model endpoint.** Chat answers `503`, and the panel and its
  button are not shown.
* **Current list prices in the repo.** See [Prices](#prices).

## Deploy with the CLI (alternative to DEPLOY)

The app folder is `backend/`.

1. Create a UC volume for the snapshots, for example `main.lakemeter.snapshots`.
2. Create the app with that volume as a resource: key `snapshot-volume`, permission
   **Can read and write**. `backend/app.yaml` reads it with `valueFrom: snapshot-volume`.
3. Upload `backend/` to the workspace and deploy it. With the CLI:
   ```bash
   databricks workspace import-dir backend /Workspace/Users/<you>/apps/lakemeter --overwrite
   databricks apps deploy <app-name> --source-code-path /Workspace/Users/<you>/apps/lakemeter
   ```
4. Open the app. The footer should read "Powered by Apps & SQLite", show the price as-of date and
   the last save time, and there should be no red banner.

The app needs no user-authorization (on-behalf-of) scopes. To use Lakebase instead, deploy with
`backend/app.lakebase.yaml` as `app.yaml` and `backend/requirements-postgres.txt` as
`requirements.txt` (upstream's own files, unchanged); the PostgreSQL path is unchanged.

| Setting (`backend/app.yaml`) | Default | Meaning |
| --- | --- | --- |
| `DB_BACKEND` | `sqlite` | `sqlite`, or unset for PostgreSQL / Lakebase |
| `SQLITE_PATH` | `/tmp/lakemeter/lakemeter.db` | Working copy (wiped on restart) |
| `LAKEMETER_SNAPSHOT_PATH` | `valueFrom: snapshot-volume` | Volume root, a `/Volumes/...` or `/Workspace/...` file, or `catalog.schema.volume` |
| `LAKEMETER_AI_ASSISTANT` | `auto` | `auto` (on only if the endpoint exists), `on` or `off` |
| `CLAUDE_MODEL_ENDPOINT` | `databricks-claude-opus-4-6` | Serving endpoint the assistant uses |

## As a child of the [App Catalog](https://github.com/The-Real-Jeremy/app-catalog)

Install **B** above does this from the browser. What it amounts to:

* `catalog.yaml` points the catalog at `backend/` (`app_dir: backend`, `mount: root`). The catalog installs the
  vendored wheels offline, serves the app at `/apps/lakemeter/`, and runs it as the catalog app's service
  principal. No user-authorization (on-behalf-of) scopes are needed.
* Persistence needs one resource on the **catalog app**: a UC volume with **Can read and write**
  (`WRITE_VOLUME`), key `lakemeter--snapshot-volume` (or `snapshot-volume`). The catalog resolves the child's
  `valueFrom: snapshot-volume` to that volume's `catalog.schema.volume` name, which the app accepts. With the
  catalog's `npm run deploy`, declare it in `catalog.overrides.yaml` instead:

  ```yaml
  lakemeter:
    resources:
      - name: snapshot-volume
        uc_securable:
          securable_full_name: <catalog>.<schema>.<volume>
          securable_type: VOLUME
          permission: WRITE_VOLUME
  ```
* The copy the catalog needs is `catalog.yaml` plus `backend/`; `DEPLOY` copies only those (and the licence
  files), so the catalog's deploy carries just the app.

## Air-gapped / UI only

The GitHub ZIP *is* the air-gapped package: carry `lakemeter-main.zip` in through your approved transfer
channel and follow **Install** above. Nothing is fetched at install or at runtime:

* `backend/requirements.txt` is `--no-index --find-links wheels`. `backend/wheels` holds the packages the
  Databricks Apps Python 3.11 runtime does not pre-install (14 pure-Python wheels); the rest are already on
  the runtime. The build log shows `Looking in links: …/wheels` and never `Looking in indexes`; a missing
  package stops the build with its name rather than hanging on the network (add it with
  `scripts/vendor_wheels.sh`).
* The frontend is prebuilt in `backend/static`; the prices are in `backend/static/pricing` and `backend/prices`.
* The AI assistant switches itself off when the workspace has no Claude serving endpoint (`LAKEMETER_AI_ASSISTANT=auto`).

For a smaller package that holds only the deployable folder, `scripts/build_airgap_zip.sh` makes
`dist/lakemeter-sqlite-app.zip` with a `MANIFEST.sha256`. Refresh the wheels after changing
`backend/requirements.txt` with `scripts/vendor_wheels.sh`, and commit.

## Prices

All prices are **USD list prices**. They are bundled with the app and loaded into the database at
start-up; nothing is fetched at runtime.

* **`backend/static/pricing/`**: the upstream snapshot, covering every cloud and region.
* **`backend/prices/`**: refreshed prices that replace snapshot rows with the same key. It
  currently holds **Azure `australiaeast` and `australiasoutheast`**:
  * Databricks DBU list prices and Linux VM prices (on demand, spot, 1-year and 3-year reserved);
  * the same DBU prices in AUD for reference (`reference/`, not loaded);
  * `PRICES.json`, which records the as-of date and sources.
* **The UI footer** shows the as-of date. Hover over it for the details.
* **`backend/prices/overrides.csv`** holds the few prices the Azure Retail Prices API lacks or
  misstates, each with its source. For example, Lakebase uses the Databricks list price (0.63 in
  `australiaeast`) where the Azure Retail meter still shows 0.32.

**To update** (one script and a commit), on a machine with internet:

```bash
python3 scripts/refresh_azure_prices.py australiaeast australiasoutheast   # any Azure regions
git add backend/prices && git commit -m "Prices as of $(date +%F)"
```

The script uses the public [Azure Retail Prices API](https://prices.azure.com/api/retail/prices)
(no sign-in). Redeploy the app and the new prices load on start (the bootstrap reloads them when
the files change). Check `overrides.csv` against your own workspace's `system.billing.list_prices`
when you refresh.

## Tests added by the fork

```bash
tests/backend_parity/run_with_throwaway_postgres.sh     # PostgreSQL vs SQLite, needs initdb/pg_ctl
python -m pytest tests/sqlite_backend                   # persistence, AI switch, snapshot paths
```

---

# Lakemeter

[![Databricks Labs](https://img.shields.io/badge/Databricks-Labs-FF3621?logo=databricks&logoColor=white)](https://www.databricks.com/learn/labs)
[![CI](https://github.com/databrickslabs/lakemeter-oss/actions/workflows/ci.yml/badge.svg)](https://github.com/databrickslabs/lakemeter-oss/actions/workflows/ci.yml)
[![Latest release](https://img.shields.io/github/v/release/databrickslabs/lakemeter-oss)](https://github.com/databrickslabs/lakemeter-oss/releases/latest)
[![Documentation](https://img.shields.io/badge/docs-online-blue)](https://databrickslabs.github.io/lakemeter-oss/)
[![GitHub stars](https://img.shields.io/github/stars/databrickslabs/lakemeter-oss?style=social)](https://github.com/databrickslabs/lakemeter-oss/stargazers)

**Estimate Databricks workload costs in minutes—with transparent assumptions you can review, share, and export.**

Lakemeter is an open-source cost estimation and workload sizing tool that runs as a **Databricks App** with built-in SSO. Configure expected usage, calculate regional costs, and export a detailed Excel estimate instead of maintaining sizing spreadsheets by hand.

![Lakemeter cost summary showing workload costs and detailed breakdowns](https://raw.githubusercontent.com/databrickslabs/lakemeter-oss/main/docs-site/static/img/gifs/cost-summary.gif)

## What you can estimate

**Compute and SQL**

- [Lakeflow Jobs](https://databrickslabs.github.io/lakemeter-oss/user-guide/jobs-compute)
- [All-Purpose Compute](https://databrickslabs.github.io/lakemeter-oss/user-guide/all-purpose-compute)
- [Lakeflow Spark Declarative Pipelines](https://databrickslabs.github.io/lakemeter-oss/user-guide/dlt-pipelines)
- [Databricks SQL warehouses](https://databrickslabs.github.io/lakemeter-oss/user-guide/dbsql-warehouses)

**AI, ML, and data services**

- [Model Serving](https://databrickslabs.github.io/lakemeter-oss/user-guide/model-serving)
- [Vector Search](https://databrickslabs.github.io/lakemeter-oss/user-guide/vector-search)
- [Databricks-hosted foundation models](https://databrickslabs.github.io/lakemeter-oss/user-guide/fmapi-databricks)
- [Proprietary foundation models](https://databrickslabs.github.io/lakemeter-oss/user-guide/fmapi-proprietary)
- [Lakebase](https://databrickslabs.github.io/lakemeter-oss/user-guide/lakebase)
- [Databricks Apps](https://databrickslabs.github.io/lakemeter-oss/user-guide/databricks-apps)
- [AI Parse](https://databrickslabs.github.io/lakemeter-oss/user-guide/ai-parse)
- [Shutterstock ImageAI](https://databrickslabs.github.io/lakemeter-oss/user-guide/shutterstock-imageai)

See the [workload sizing catalog](https://databrickslabs.github.io/lakemeter-oss/user-guide/workloads) for the inputs and calculation behavior of every workload.

## Why Lakemeter

- **Transparent calculations** — Review usage quantities, billing units, SKUs, rates, VM costs, storage, and discounts behind every total.
- **Workload-specific sizing** — Use forms tailored to each supported Databricks workload rather than a generic calculator.
- **AI-assisted estimates** — Describe a workload in natural language, review the suggested configuration, and accept it with one click.
- **Excel export** — Generate a detailed workbook for customer conversations, procurement reviews, RFPs, and internal planning.
- **Cloud and region awareness** — Model the cloud, region, tier, and pricing options available in the app.
- **Built for teams** — Create, duplicate, share, and compare multi-workload estimates in a Databricks workspace.

## Quick start

You need a Databricks workspace and a configured [Databricks CLI profile](https://docs.databricks.com/aws/en/dev-tools/cli/profiles.html).

```bash
git clone https://github.com/databrickslabs/lakemeter-oss.git
cd lakemeter-oss

./scripts/install.sh --profile <your-cli-profile>
```

The one-command installer provisions Lakebase, loads pricing data, deploys the app, and verifies the installation. It typically completes in **5–15 minutes**.

For permissions, deployment inventory, non-interactive installation, and troubleshooting, see the [installation guide](https://databrickslabs.github.io/lakemeter-oss/admin-guide/installer).

## See the workflow

Add workload assumptions and calculate their estimated cost:

![Adding and configuring a workload](https://raw.githubusercontent.com/databrickslabs/lakemeter-oss/main/docs-site/static/img/gifs/adding-workload.gif)

Export the completed estimate to Excel:

![Exporting a Lakemeter estimate to Excel](https://raw.githubusercontent.com/databrickslabs/lakemeter-oss/main/docs-site/static/img/gifs/export-excel.gif)

## Documentation

- [User guide](https://databrickslabs.github.io/lakemeter-oss/user-guide/overview) — Create estimates, inspect pricing, use AI assistance, and export
- [Workload sizing guides](https://databrickslabs.github.io/lakemeter-oss/user-guide/workloads) — Inputs and calculation behavior for every supported workload
- [Admin guide](https://databrickslabs.github.io/lakemeter-oss/admin-guide/deployment) — Installation, architecture, permissions, and API reference
- [Changelog](https://databrickslabs.github.io/lakemeter-oss/changelog) — Releases and upgrade notes

## Contributing

Bug reports, feature requests, documentation improvements, and pull requests are welcome. Read [CONTRIBUTING.md](CONTRIBUTING.md) before contributing, or [open an issue](https://github.com/databrickslabs/lakemeter-oss/issues/new) to start a discussion.

If Lakemeter helps you size or explain a Databricks workload, consider [starring the repository](https://github.com/databrickslabs/lakemeter-oss) so others can discover it.

## Technology

- **Frontend:** React, TypeScript, Tailwind CSS, Vite
- **Backend:** FastAPI, SQLAlchemy, Pydantic
- **Database:** Lakebase (managed PostgreSQL on Databricks)
- **AI:** Claude through Databricks Foundation Model APIs
- **Hosting:** Databricks Apps with SSO and managed compute

## License

Copyright (2026) Databricks, Inc. This software includes software developed at Databricks and is subject to [LICENSE.md](LICENSE.md). Third-party dependency notices are provided in [NOTICE.md](NOTICE.md).
