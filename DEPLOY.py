# Databricks notebook source
# MAGIC %md
# MAGIC # Deploy Lakemeter (SQLite) (browser only)
# MAGIC
# MAGIC Two ways, chosen with the **mode** widget:
# MAGIC - **child**: add it to an App Catalog that you deployed from `app-catalog-main/` (the default).
# MAGIC   It copies the app into the catalog's `child_apps/lakemeter/` and redeploys the catalog. It never creates
# MAGIC   or changes the catalog app; it tells you if the catalog app still needs the snapshot volume.
# MAGIC - **standalone**: deploy it as its own Databricks App, with a UC volume that keeps the estimates.
# MAGIC
# MAGIC Needs no CLI, pip or npm: only the Databricks SDK the notebook already has. The app installs offline from
# MAGIC the wheels in `backend/wheels`.

# COMMAND ----------

import os, re, shutil, time, json
ctx = dbutils.notebook.entry_point.getDbutils().notebook().getContext()
SRC = "/Workspace" + os.path.dirname(ctx.notebookPath().get())
PARENT = os.path.dirname(SRC)
# Prefer the folder named exactly app-catalog-main (an old copy renamed aside must not win).
_cands = [d for d in sorted(os.listdir(PARENT)) if d.startswith("app-catalog") and os.path.isfile(os.path.join(PARENT, d, "catalog", "server.mjs"))]
guess = os.path.join(PARENT, "app-catalog-main") if "app-catalog-main" in _cands else (os.path.join(PARENT, _cands[0]) if _cands else "")

dbutils.widgets.dropdown("mode", "child", ["child", "standalone"], "1. Mode")
dbutils.widgets.text("catalog_folder", guess, "2. (child) App Catalog folder")
dbutils.widgets.text("catalog_app_name", "app-catalog", "3. (child) App Catalog app name")
dbutils.widgets.text("standalone_app_name", "lakemeter", "4. (standalone) App name")
dbutils.widgets.text("snapshot_volume", "", "5. Snapshot volume <catalog>.<schema>.<volume>")
dbutils.widgets.text("budget_policy_id", "", "6. Budget policy id, if your workspace requires one")
MODE = dbutils.widgets.get("mode")
VOLUME = dbutils.widgets.get("snapshot_volume").strip()
CHILD = "lakemeter"
APP_ROOT = os.path.join(SRC, "backend")      # what a standalone deploy ships
RESOURCE = "snapshot-volume"                  # app.yaml: LAKEMETER_SNAPSHOT_PATH valueFrom snapshot-volume
print("this folder:", SRC, "| mode:", MODE)
assert os.path.isfile(os.path.join(APP_ROOT, "app.yaml")) and os.path.isdir(os.path.join(APP_ROOT, "wheels")), \
    f"{APP_ROOT} is missing app.yaml or wheels/: was the whole repo imported?"
assert not VOLUME or re.fullmatch(r"[^.\s]+\.[^.\s]+\.[^.\s]+", VOLUME), \
    "snapshot_volume must be a three-part name: <catalog>.<schema>.<volume>"

# COMMAND ----------

from databricks.sdk import WorkspaceClient
from databricks.sdk.errors import NotFound
api = WorkspaceClient().api_client

def get_app(name):
    try:
        return api.do("GET", f"/api/2.0/apps/{name}")
    except NotFound:
        return None

def wait_compute(name, states, minutes=15):
    st = None
    for _ in range(minutes * 12):
        a = get_app(name)
        st = (a.get("compute_status") or {}).get("state")
        if st in states:
            return a
        time.sleep(5)
    raise TimeoutError(f"compute of {name} did not reach {states}; last {st}")

# ── Budget / usage policies ──────────────────────────────────────────────────────────────────────────
# A workspace can require every serverless resource (including an app) to name a budget policy; then a
# create without one fails with "Unable to use fallback policies, please manually select a policy".
# Policies are account objects with no workspace list API, so the candidates are the policy ids already
# in use on apps and jobs you can see.
POLICY_ERR = "fallback polic"

def policy_candidates():
    ids = {}
    try:
        tok = None
        while True:
            r = api.do("GET", "/api/2.0/apps", query={"page_size": 100, **({"page_token": tok} if tok else {})})
            for a in r.get("apps", []):
                for k in ("budget_policy_id", "effective_budget_policy_id"):
                    if a.get(k):
                        ids.setdefault(a[k], set()).add(f"app {a['name']}")
            tok = r.get("next_page_token")
            if not tok:
                break
    except Exception as e:
        print("  (could not list apps:", str(e)[:100], ")")
    try:
        r = api.do("GET", "/api/2.1/jobs/list", query={"limit": 100})
        for j in r.get("jobs", []):
            for k in ("budget_policy_id", "effective_budget_policy_id"):
                v = j.get(k) or (j.get("settings") or {}).get(k)
                if v:
                    ids.setdefault(v, set()).add(f"job {(j.get('settings') or {}).get('name', j.get('job_id'))}")
    except Exception as e:
        print("  (could not list jobs:", str(e)[:100], ")")
    return ids

def keep_policy(a, body):
    """A PATCH is a full replacement: carry the app's policy over, or the update falls back and fails."""
    for k, eff in (("budget_policy_id", "effective_budget_policy_id"), ("usage_policy_id", "effective_usage_policy_id")):
        v = a.get(k) or a.get(eff)
        if v:
            body[k] = v
    return body

def create_app(name, description, resources, policy_id):
    body = {"name": name, "description": description, "resources": resources}
    if policy_id:
        body["budget_policy_id"] = policy_id
    try:
        api.do("POST", "/api/2.0/apps", body=body)
        return
    except Exception as e:
        if POLICY_ERR not in str(e) or policy_id:
            raise
        print("This workspace requires a budget policy on new apps. Looking for the ones in use …")
    cands = policy_candidates()
    for pid, users in cands.items():
        print(f"  {pid}   used by: {', '.join(sorted(users))[:120]}")
    if len(cands) == 1:
        pid = next(iter(cands))
        print("Exactly one policy is in use; using it:", pid)
        body["budget_policy_id"] = pid
        api.do("POST", "/api/2.0/apps", body=body)
        return
    raise RuntimeError(
        "Pick a budget policy: paste one of the ids above into the budget_policy_id widget and Run all again, "
        "OR create the app in the UI (Compute → Apps → Create app → Custom, name '" + name + "', choose the policy, "
        "add a UC volume resource with key '" + RESOURCE + "' and permission Can read and write) and Run all again.")

def volume_resource(full_name):
    return {"name": RESOURCE, "description": "durable copy of the estimates (SQLite snapshot)",
            "uc_securable": {"securable_type": "VOLUME", "securable_full_name": full_name, "permission": "WRITE_VOLUME"}}

def ensure_volume(full_name):
    """Create the schema and the volume if they are missing (needs the usual UC privileges)."""
    catalog, schema, volume = full_name.split(".")
    try:
        api.do("GET", f"/api/2.1/unity-catalog/volumes/{full_name}")
        print("snapshot volume exists:", full_name)
        return
    except NotFound:
        pass
    try:
        api.do("GET", f"/api/2.1/unity-catalog/schemas/{catalog}.{schema}")
    except NotFound:
        print(f"creating schema {catalog}.{schema} …")
        api.do("POST", "/api/2.1/unity-catalog/schemas", body={"catalog_name": catalog, "name": schema,
                                                               "comment": "Lakemeter (SQLite) snapshots"})
    print(f"creating volume {full_name} …")
    api.do("POST", "/api/2.1/unity-catalog/volumes", body={
        "catalog_name": catalog, "schema_name": schema, "name": volume, "volume_type": "MANAGED",
        "comment": "Lakemeter (SQLite): durable copy of the estimates"})

def deploy(name, source):
    a = get_app(name)
    if (a.get("compute_status") or {}).get("state") != "ACTIVE":
        print("starting compute …")
        try:
            api.do("POST", f"/api/2.0/apps/{name}/start", body={})
        except Exception as e:
            print("  start:", str(e)[:120])
        wait_compute(name, {"ACTIVE"})
    print(f"deploying {source} → {name} …")
    dep = api.do("POST", f"/api/2.0/apps/{name}/deployments", body={"source_code_path": source, "mode": "SNAPSHOT"})
    t0 = time.time()
    while True:
        d = api.do("GET", f"/api/2.0/apps/{name}/deployments/{dep['deployment_id']}")
        st = (d.get("status") or {}).get("state")
        if st not in ("IN_PROGRESS", "PENDING", None):
            break
        if time.time() - t0 > 25 * 60:
            raise TimeoutError("deployment still running after 25 min: check Compute → Apps → Logs")
        time.sleep(10)
    print(f"deployment: {st} — {(d.get('status') or {}).get('message')} ({time.time() - t0:.0f}s)")
    assert st == "SUCCEEDED", "deployment failed: open Compute → Apps → this app → Logs"
    return get_app(name)

def try_json(url):
    """GET with this notebook's token. The Apps proxy may answer it with a sign-in page (HTML): not data."""
    try:
        import urllib.request
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {ctx.apiToken().get()}"})
        with urllib.request.urlopen(req, timeout=90) as r:
            raw = r.read().decode("utf-8", "replace")
        return json.loads(raw) if raw.lstrip().startswith("{") else None
    except Exception:
        return None

def report_health(base, label):
    p = try_json(f"{base}/api/v1/system/persistence")
    pr = try_json(f"{base}/api/v1/system/prices")
    f = try_json(f"{base}/api/v1/system/features")
    if p is None:
        print(f"(open {base}/api/v1/system/persistence in the browser: \"backend\": \"sqlite\", \"healthy\": true)")
        return
    print(f"{label}: backend={p.get('backend')} healthy={p.get('healthy')} restore={p.get('restore')}"
          f" snapshot={p.get('snapshot_path')}")
    if not p.get("healthy"):
        print("  !", p.get("restore_detail") or p.get("last_error"))
    if pr:
        print("  prices as of:", (pr.get("refreshed") or {}).get("as_of"), "| snapshot DBU", pr["snapshot"]["dbu_as_of"])
    if f:
        print("  AI assistant:", "on" if f["ai_assistant"]["enabled"] else "off", "-", f["ai_assistant"]["reason"])

# COMMAND ----------

if MODE == "child":
    CAT = dbutils.widgets.get("catalog_folder").strip().rstrip("/")
    CAT_APP = dbutils.widgets.get("catalog_app_name").strip()
    assert CAT and os.path.isfile(f"{CAT}/catalog/server.mjs"), \
        f"catalog_folder must be the App Catalog folder (with catalog/server.mjs); got {CAT!r}. Import app-catalog-main.zip first."
    cat = get_app(CAT_APP)
    assert cat is not None, f"the App Catalog app {CAT_APP!r} does not exist: deploy the App Catalog first (its DEPLOY notebook)"
    dest = f"{CAT}/child_apps/{CHILD}"
    if os.path.exists(dest):   # keep the previous copy, OUTSIDE child_apps so it is not discovered as an app
        bak = f"{CAT}/.bak-child-{CHILD}-{time.strftime('%Y%m%d-%H%M%S')}"
        os.rename(dest, bak)
        for _ in range(30):     # /Workspace FUSE: a renamed path lingers for a few seconds
            if not os.path.exists(dest):
                break
            time.sleep(1)
        print("previous copy kept at", bak)
    # Only what the catalog runs: catalog.yaml points it at backend/ (app_dir). The rest of the repo
    # (frontend sources, tests, scripts) stays out of the catalog's deploy.
    os.makedirs(dest)
    for f in ("catalog.yaml", "README.md", "LICENSE.md", "NOTICE.md", "FORK_CHANGES.md"):
        shutil.copy2(f"{SRC}/{f}", f"{dest}/{f}")
    shutil.copytree(APP_ROOT, f"{dest}/backend", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    n_src = 5 + sum(len([x for x in fs if not x.endswith(".pyc")]) for r, _, fs in os.walk(APP_ROOT) if "__pycache__" not in r)
    n_dst = sum(len(fs) for _, _, fs in os.walk(dest))
    assert n_dst == n_src, f"copied {n_dst} of {n_src} files into {dest}"
    print(f"copied {n_dst} files into {dest}")

    # Persistence: the catalog resolves valueFrom "snapshot-volume" against the catalog app's own
    # resources named "lakemeter--snapshot-volume" or "snapshot-volume". This notebook does not change the
    # catalog app; it checks and tells you.
    vols = [r for r in (cat.get("resources") or []) if r.get("name") in (f"{CHILD}--{RESOURCE}", RESOURCE)
            and (r.get("uc_securable") or {}).get("securable_type") == "VOLUME"]
    if vols:
        print("snapshot volume on the catalog app:", vols[0]["uc_securable"]["securable_full_name"],
              "(", vols[0]["uc_securable"].get("permission"), ")")
    else:
        print("\n!! The catalog app has no snapshot volume, so Lakemeter will run but NOT keep estimates (red banner).")
        print(f"   Add it in the UI: Compute → Apps → {CAT_APP} → Edit → App resources → + Add resource → UC volume,")
        print(f"   volume {VOLUME or '<catalog>.<schema>.<volume>'}, permission Can read and write, key '{CHILD}--{RESOURCE}'.")
        print("   Then Run all again (or redeploy the catalog).\n")
    a = deploy(CAT_APP, CAT)
    url = a.get("url")
    print("\nCATALOG URL:", url)
    print("THIS APP   :", f"{url}/apps/{CHILD}/")
    h = try_json(f"{url}/_catalog/api/health")
    if h is not None:
        mine = [c for c in h.get("children", []) if c["name"] == CHILD]
        print("catalog health: offline =", h.get("offline"), "|", CHILD, "→",
              mine[0].get("prepare") if mine else "NOT FOUND", mine[0].get("problems") if mine else "")
    else:
        print("(open", f"{url}/_catalog/api/health", "in the browser: this app should show \"prepare\": \"ready\")")
    report_health(f"{url}/apps/{CHILD}", "lakemeter")
else:
    NAME = dbutils.widgets.get("standalone_app_name").strip()
    assert NAME and NAME == NAME.lower() and NAME.replace("-", "").isalnum(), "app name: lowercase letters, digits and '-'"
    assert VOLUME, "standalone mode needs snapshot_volume (<catalog>.<schema>.<volume>): it keeps the estimates"
    ensure_volume(VOLUME)
    a = get_app(NAME)
    if a is None:
        print(f"creating app {NAME} …")
        create_app(NAME, "Lakemeter (SQLite): Databricks pricing calculator", [volume_resource(VOLUME)],
                   dbutils.widgets.get("budget_policy_id").strip())
        a = wait_compute(NAME, {"ACTIVE", "STOPPED", "ERROR"})
    resources = [r for r in (a.get("resources") or []) if r.get("name") != RESOURCE]
    current = next((r for r in (a.get("resources") or []) if r.get("name") == RESOURCE), None)
    if (current or {}).get("uc_securable", {}).get("securable_full_name") != VOLUME:
        a = wait_compute(NAME, {"ACTIVE", "STOPPED", "ERROR"})
        body = keep_policy(a, {"name": NAME, "description": a.get("description", ""),
                               "user_api_scopes": a.get("user_api_scopes") or [],
                               "resources": resources + [volume_resource(VOLUME)]})
        api.do("PATCH", f"/api/2.0/apps/{NAME}", body=body)
        print(f"{NAME}: resource '{RESOURCE}' → {VOLUME}")
    else:
        print(f"{NAME} already keeps its estimates in {VOLUME}")
    a = deploy(NAME, APP_ROOT)
    url = a.get("url")
    print("\nAPP URL:", url)
    report_health(url, "lakemeter")
