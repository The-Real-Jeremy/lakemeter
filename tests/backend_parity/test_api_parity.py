"""The whole HTTP surface must answer identically on PostgreSQL and SQLite.

Two live servers get the same requests: every calculate endpoint across a
payload grid (valid and invalid), every reference endpoint, and a golden set
of estimates holding one line item per workload type, read back through the
estimate, line-item and Excel export endpoints. Status codes and response
bodies must be equal after removing only ids and timestamps.
"""
from __future__ import annotations

import io
import itertools
import random
import re

import pytest
from sqlalchemy import text

UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
TIMESTAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:?\d{2})?")
VOLATILE_KEYS = {"created_at", "updated_at", "last_login_at", "calculation_completed_at",
                 "last_accessed_at", "bootstrapped_at"}


def normalise(value):
    if isinstance(value, dict):
        return {k: ("<volatile>" if k in VOLATILE_KEYS else normalise(v))
                for k, v in value.items()}
    if isinstance(value, list):
        return [normalise(v) for v in value]
    if isinstance(value, str):
        return TIMESTAMP_RE.sub("<ts>", UUID_RE.sub("<uuid>", value))
    return value


def body(response):
    try:
        return normalise(response.json())
    except ValueError:
        return response.text


def unordered_line_items(payload):
    """``Estimate.line_items`` is a relationship with no ORDER BY, so the API
    makes no promise about its order on either backend; compare it by
    display_order. (``/estimates/{id}/full`` and ``/line-items/estimate/{id}``
    do ORDER BY display_order and are compared as returned.)"""
    if isinstance(payload, dict) and isinstance(payload.get("line_items"), list):
        payload = dict(payload, line_items=sorted(
            payload["line_items"], key=lambda i: (i.get("display_order") or 0, repr(i))))
    return payload


def same(clients, method, path, **kwargs):
    pg, sq = clients
    a = pg.request(method, path, **kwargs)
    b = sq.request(method, path, **kwargs)
    return (a.status_code, body(a)), (b.status_code, body(b))


def _rows(engine, sql):
    with engine.connect() as conn:
        return [tuple(r) for r in conn.execute(text(sql))]


# ---------------------------------------------------------------------------
# Calculate endpoints
# ---------------------------------------------------------------------------

def calculate_cases(engine) -> list[tuple[str, dict]]:
    rng = random.Random(51105)
    cases: list[tuple[str, dict]] = []
    locations = [
        ("aws", "us-east-1", "PREMIUM"), ("aws", "ap-southeast-2", "ENTERPRISE"),
        ("azure", "australiaeast", "PREMIUM"), ("azure", "eastus", "STANDARD"),
        ("gcp", "us-central1", "PREMIUM"), ("aws", "nowhere-1", "PREMIUM"),
    ]
    usages = [
        dict(runs_per_day=3, avg_runtime_minutes=7, days_per_month=30),
        dict(hours_per_month=100.5), dict(hours_per_month=730),
        dict(runs_per_day=1, avg_runtime_minutes=45), dict(),
    ]
    hour_usages = [dict(hours_per_day=8, days_per_month=22), dict(hours_per_month=101.5), dict()]
    discount = {"global_discount_pct": 12.5}

    for cloud, region, tier in locations:
        loc = dict(cloud=cloud, region=region, tier=tier)
        vm = _rows(engine, f"""
            SELECT DISTINCT instance_type, pricing_tier, payment_option
            FROM lakemeter.sync_pricing_vm_costs
            WHERE UPPER(cloud) = '{cloud.upper()}' AND region = '{region}'""")
        nodes = sorted({r[0] for r in vm}) or ["i3.xlarge"]
        nodes = rng.sample(nodes, min(4, len(nodes)))
        tiers = sorted({(r[1], r[2]) for r in vm}) or [("on_demand", "NA")]
        tiers = rng.sample(tiers, min(3, len(tiers)))

        for endpoint, extra in (("jobs-classic", usages), ("dlt-classic", usages),
                                ("all-purpose-classic", hour_usages)):
            for node, workers, photon, (wt, wp) in itertools.product(
                    nodes[:3], (0, 2), (False, True), tiers[:2]):
                payload = dict(loc, driver_node_type=node, worker_node_type=rng.choice(nodes),
                               num_workers=workers, photon_enabled=photon,
                               worker_pricing_tier=wt, worker_payment_option=wp,
                               **rng.choice(extra))
                if endpoint == "dlt-classic":
                    payload["dlt_edition"] = rng.choice(["CORE", "PRO", "ADVANCED"])
                if rng.random() < 0.3:
                    payload["discount_config"] = discount
                cases.append((endpoint, payload))
        for endpoint, extra in (("jobs-serverless", usages), ("dlt-serverless", usages),
                                ("all-purpose-serverless", hour_usages)):
            for mode in ("standard", "performance"):
                payload = dict(loc, driver_node_type=rng.choice(nodes),
                               worker_node_type=rng.choice(nodes), num_workers=rng.choice([1, 4]),
                               serverless_mode=mode, **rng.choice(extra))
                cases.append((endpoint, payload))
        for wtype, size in itertools.product(("classic", "pro"), ("2X-Small", "Small", "Large")):
            cases.append(("dbsql-classic-pro", dict(loc, warehouse_type=wtype, warehouse_size=size,
                                                    **rng.choice(hour_usages))))
        for size in ("2X-Small", "Medium", "4X-Large"):
            cases.append(("dbsql-serverless", dict(loc, warehouse_size=size, **rng.choice(hour_usages))))
        for mode, millions in itertools.product(("standard", "storage_optimized"), (1, 3, 64, 250.5)):
            cases.append(("vector-search", dict(loc, mode=mode, num_vectors_millions=millions,
                                                storage_gb=rng.choice([0, 50]),
                                                **rng.choice(hour_usages[:2]))))
        gpus = [r[0] for r in _rows(engine, f"""
            SELECT DISTINCT size_or_model FROM lakemeter.sync_product_serverless_rates
            WHERE UPPER(cloud) = '{cloud.upper()}' AND product = 'model_serving'""")]
        for gpu in gpus[:4] + ["cpu"]:
            cases.append(("model-serving", dict(loc, gpu_type=gpu, scale_out=rng.choice(["small", "medium"]),
                                                **rng.choice(hour_usages))))
        db_models = [r[0] for r in _rows(engine, f"""
            SELECT DISTINCT model FROM lakemeter.sync_product_fmapi_databricks
            WHERE UPPER(cloud) = '{cloud.upper()}' ORDER BY 1""")]
        for model in rng.sample(db_models, min(3, len(db_models))):
            cases.append(("fmapi-databricks", dict(loc, model=model, input_tokens_per_month=5_000_000,
                                                   output_tokens_per_month=750_000)))
        prop = _rows(engine, f"""
            SELECT DISTINCT provider, model FROM lakemeter.sync_product_fmapi_proprietary
            WHERE UPPER(cloud) = '{cloud.upper()}' ORDER BY 1, 2""")
        for provider, model in rng.sample(prop, min(3, len(prop))):
            cases.append(("fmapi-proprietary", dict(loc, provider=provider, model=model,
                                                    input_tokens_per_month=2_000_000,
                                                    output_tokens_per_month=400_000)))
        for cu in (0.5, 4, 32):
            cases.append(("lakebase", dict(loc, cu_size=cu, storage_gb=100, read_replicas=1,
                                           **rng.choice(hour_usages))))
        cases += [
            ("databricks-apps", dict(loc, size="medium", num_apps=3, hours_per_month=200)),
            ("ai-parse", dict(loc, mode="pages", complexity="medium", pages_thousands=12)),
            ("ai-runtime", dict(loc, accelerator_type="GPU_1xA10", hours_per_month=40)),
            ("ai-classify", dict(loc, num_docs=10000)),
            ("ai-extract", dict(loc, num_inputs=5000)),
            ("ai-gateway", dict(loc, usage_tracking_enabled=True, usage_tracking_input_method="payload_gb",
                                usage_tracking_monthly_payload_gb=10)),
            ("agent-evaluation", dict(loc, labels_enabled=True, input_tokens_millions=3,
                                      output_tokens_millions=1)),
            ("general-storage", dict(loc, quantity=500, unit="gb", tier_1_operations_thousands=20)),
            ("lakeflow-connect", dict(loc, runs_per_day=4, avg_runtime_minutes=20, gateway_enabled=True,
                                      gateway_instance_type=nodes[0], gateway_hours_per_month=720)),
            ("shutterstock-imageai", dict(loc, images_per_month=1200)),
            ("zerobus", dict(loc, monthly_ingested_gb=300)),
            ("platform-addon", dict(cloud=cloud, tier=tier, addon_type="ENHANCED_SECURITY_COMPLIANCE",
                                    product_spend_at_list=25000, discount_pct=10)),
            ("platform-addon", dict(cloud=cloud, tier=tier, addon_type="MISSION_CRITICAL",
                                    product_spend_at_list=90000)),
            # invalid requests must fail the same way
            ("jobs-classic", dict(loc, driver_node_type="no-such-node", num_workers=1)),
            ("dbsql-classic-pro", dict(loc, warehouse_type="bogus", warehouse_size="Small")),
            ("lakebase", dict(loc, cu_size=-1)),
        ]
    return cases


@pytest.fixture(scope="module")
def calc_cases(sqlite_engine):
    generated = calculate_cases(sqlite_engine)
    assert len(generated) > 300
    return generated


def test_calculate_endpoints_identical(clients, calc_cases):
    mismatches, statuses = [], {}
    for endpoint, payload in calc_cases:
        pg, sq = same(clients, "POST", f"/calculate/{endpoint}", json=payload)
        statuses.setdefault(endpoint, set()).add(pg[0])
        if pg != sq:
            mismatches.append((endpoint, payload, pg, sq))
    ok = sum(1 for e, p in calc_cases)
    print(f"\ncalculate parity: {ok} requests over {len(statuses)} endpoints; statuses {statuses}")
    assert not mismatches, "\n".join(
        f"{e} {p}\n  pg={a}\n  sq={b}" for e, p, a, b in mismatches[:3])
    never = sorted(e for e, s in statuses.items() if 200 not in s)
    assert not never, f"endpoints never priced anything (payloads wrong?): {never}"


REFERENCE = [
    "/workload-types", "/workload-types/JOBS", "/vm-pricing/tiers", "/vm-pricing/payment-options",
    "/regions", "/regions?cloud=aws", "/tiers?cloud=azure", "/instances/families",
    "/instances/vm-pricing-options?cloud=aws", "/dbsql/warehouse-types", "/dlt/editions",
    "/vector-search/list", "/lakebase/list", "/serverless/modes", "/fmapi/databricks-models/list",
    "/fmapi-databricks", "/fmapi-proprietary",
    "/vm-pricing?cloud=aws&region=us-east-1&pricing_tier=on_demand",
    "/vm-pricing/regions?cloud=gcp", "/vm-pricing/instance-types?cloud=azure&region=australiaeast",
    "/vm-pricing/price?cloud=aws&region=us-east-1&instance_type=i3.xlarge",
    "/instances/types?cloud=aws&region=us-east-1&min_vcpus=4&max_vcpus=16&limit=25",
    "/instances/types?cloud=azure&region=australiaeast&instance_family=General%20Purpose",
    "/instances/vm-costs?cloud=aws&region=us-east-1&instance_type=m5.xlarge",
    "/dbsql/warehouse-sizes?cloud=aws", "/dbsql/warehouse-sizes?cloud=aws&warehouse_type=pro",
    "/dbsql/warehouse-vm-costs?cloud=aws&region=us-east-1&warehouse_type=classic&warehouse_size=Small",
    "/dbsql/warehouse-hardware?cloud=azure&warehouse_type=pro&warehouse_size=Medium",
    "/vector-search/modes?cloud=aws", "/vector-search/modes?cloud=aws&mode=standard",
    "/lakebase/calculate?cu_size=4&cloud=aws&tier=PREMIUM&read_replicas=1",
    "/photon/list?cloud=aws", "/photon/multipliers?cloud=gcp",
    "/pricing/product-types?cloud=aws&region=us-east-1&tier=PREMIUM",
    "/pricing/dbu-rates?cloud=azure&region=australiaeast&tier=PREMIUM",
    "/pricing/dbu-rates?cloud=aws&region=us-east-1&tier=PREMIUM&product_type=JOBS_COMPUTE",
    "/model-serving/gpu-types?cloud=aws",
    "/fmapi/proprietary-models/list?provider=anthropic",
    "/fmapi/proprietary-models?provider=openai&cloud=aws",
    "/fmapi/databricks-models?model=bge-large&cloud=aws",
]


def _sorted_lists(value):
    if isinstance(value, tuple):
        return tuple(_sorted_lists(v) for v in value)
    if isinstance(value, dict):
        return {k: _sorted_lists(v) for k, v in value.items()}
    if isinstance(value, list):
        return sorted((_sorted_lists(v) for v in value), key=repr)
    return value


@pytest.fixture(scope="module")
def pg_byte_order(pg_engine) -> bool:
    """Whether the PostgreSQL database sorts text by byte order, as SQLite does."""
    with pg_engine.connect() as conn:
        collation = conn.execute(text(
            "SELECT datcollate FROM pg_database WHERE datname = current_database()")).scalar()
    return collation.upper() in ("C", "POSIX", "C.UTF-8", "C.UTF8")


@pytest.mark.parametrize("path", REFERENCE)
def test_reference_endpoint_identical(clients, path, pg_byte_order):
    pg, sq = same(clients, "GET", path)
    if pg_byte_order:
        assert pg == sq
        return
    # A locale-collated PostgreSQL orders text differently from SQLite's byte
    # order (e.g. en_AU puts "General Purpose" before "GPU"). That changes
    # the order of a dropdown, never a value; prove nothing else differs.
    assert _sorted_lists(pg) == _sorted_lists(sq)


# ---------------------------------------------------------------------------
# Golden estimates: one line item per workload type, read back every way
# ---------------------------------------------------------------------------

LINE_ITEMS = [
    dict(workload_type="JOBS", driver_node_type="m5d.xlarge", worker_node_type="i3.xlarge",
         num_workers=3, photon_enabled=True, runs_per_day=4, avg_runtime_minutes=35,
         worker_pricing_tier="spot"),
    dict(workload_type="JOBS", serverless_enabled=True, serverless_mode="performance",
         driver_node_type="i3.xlarge", worker_node_type="i3.xlarge", num_workers=2,
         runs_per_day=10, avg_runtime_minutes=12),
    dict(workload_type="ALL_PURPOSE", driver_node_type="i3.2xlarge", worker_node_type="i3.2xlarge",
         num_workers=2, hours_per_month=160),
    dict(workload_type="DLT", dlt_edition="ADVANCED", driver_node_type="i3.xlarge",
         worker_node_type="i3.xlarge", num_workers=2, hours_per_month=300),
    dict(workload_type="DBSQL", dbsql_warehouse_type="PRO", dbsql_warehouse_size="Medium",
         dbsql_num_clusters=2, hours_per_month=220),
    dict(workload_type="DBSQL", dbsql_warehouse_type="SERVERLESS", dbsql_warehouse_size="Small",
         hours_per_month=180),
    dict(workload_type="VECTOR_SEARCH", vector_search_mode="standard", vector_capacity_millions=5),
    dict(workload_type="MODEL_SERVING", model_serving_gpu_type="gpu_small_t4", hours_per_month=730),
    dict(workload_type="FMAPI_DATABRICKS", fmapi_model="bge-large", fmapi_rate_type="input_token",
         fmapi_quantity=20_000_000),
    dict(workload_type="FMAPI_PROPRIETARY", fmapi_provider="anthropic", fmapi_model="claude-sonnet-4",
         fmapi_endpoint_type="global", fmapi_context_length="all", fmapi_rate_type="output_token",
         fmapi_quantity=3_000_000),
    dict(workload_type="LAKEBASE", lakebase_cu=4, lakebase_storage_gb=200, lakebase_ha_nodes=2),
    dict(workload_type="DATABRICKS_APPS", databricks_apps_size="medium", databricks_apps_num_apps=2,
         databricks_apps_hours_per_month=300),
    dict(workload_type="AI_PARSE", ai_parse_mode="pages", ai_parse_complexity="medium",
         ai_parse_pages_thousands=8),
    dict(workload_type="LAKEFLOW_CONNECT", lakeflow_connect_connector_type="salesforce",
         runs_per_day=4, avg_runtime_minutes=15),
    dict(workload_type="ZEROBUS", workload_config={"zerobus_mode": "standard",
                                                   "zerobus_monthly_ingested_gb": 120}),
    dict(workload_type="GENERAL_STORAGE", workload_config={
        "general_storage_quantity": 2, "general_storage_unit": "tb",
        "general_storage_tier1_operations_thousands": 40}),
    dict(workload_type="SHUTTERSTOCK_IMAGEAI", shutterstock_imageai_num_images=900),
    dict(workload_type="AI_EXTRACT", workload_config={"ai_extract_document_type": "invoice",
                                                      "ai_extract_num_inputs": 7000}),
    dict(workload_type="AI_CLASSIFY", workload_config={"ai_classify_document_type": "short_text",
                                                       "ai_classify_num_docs": 20000}),
    dict(workload_type="AI_GATEWAY", workload_config={
        "ai_gateway_usage_tracking_enabled": True,
        "ai_gateway_usage_tracking_input_method": "payload_gb",
        "ai_gateway_usage_tracking_monthly_payload_gb": 15}),
    dict(workload_type="AGENT_EVALUATION", workload_config={
        "agent_evaluation_labels_enabled": True,
        "agent_evaluation_synthetic_data_enabled": False,
        "agent_evaluation_input_tokens_millions": 4,
        "agent_evaluation_output_tokens_millions": 1}),
    dict(workload_type="AI_RUNTIME", hours_per_month=60,
         workload_config={"ai_runtime_accelerator_type": "GPU_1xA10"}),
]


@pytest.fixture(scope="module")
def golden(clients, run_id):
    """(pg_estimate_id, sqlite_estimate_id) for an estimate built identically on both."""
    pg, sq = clients
    workload_types = [w["workload_type"] for w in pg.get("/workload-types").json()]
    ids = []
    for client in (pg, sq):
        created = client.post("/estimates/", json=dict(
            estimate_name=f"golden-{run_id}", customer_name="Parity", cloud="aws",
            region="us-east-1", tier="PREMIUM", discount_config={"global_discount_pct": 15}))
        assert created.status_code == 201, created.text
        estimate_id = created.json()["estimate_id"]
        for order, item in enumerate(LINE_ITEMS):
            response = client.post("/line-items/", json=dict(
                item, estimate_id=estimate_id, display_order=order,
                workload_name=f"{item['workload_type'].lower()}-{order}"))
            assert response.status_code == 201, (item, response.text)
        ids.append(estimate_id)
    covered = {i["workload_type"] for i in LINE_ITEMS}
    return ids, covered, workload_types


def test_golden_covers_every_workload_type(golden):
    _, covered, workload_types = golden
    missing = set(workload_types) - covered
    assert not missing, f"golden estimate lacks workload types {missing}"


@pytest.mark.parametrize("path", ["/estimates/{id}", "/estimates/{id}/full",
                                  "/line-items/estimate/{id}"])
def test_golden_estimate_reads_identical(clients, golden, path):
    (pg_id, sq_id), _, _ = golden
    pg, sq = clients
    a, b = pg.get(path.format(id=pg_id)), sq.get(path.format(id=sq_id))
    assert a.status_code == b.status_code == 200
    assert unordered_line_items(body(a)) == unordered_line_items(body(b))


def _cells(content: bytes):
    import openpyxl
    book = openpyxl.load_workbook(io.BytesIO(content), data_only=False)
    return {ws.title: [[normalise(c.value) if isinstance(c.value, str) else c.value
                        for c in row] for row in ws.iter_rows()]
            for ws in book.worksheets}


def test_golden_excel_export_identical(clients, golden):
    (pg_id, sq_id), _, _ = golden
    pg, sq = clients
    a = pg.get(f"/export/estimate/{pg_id}/excel")
    b = sq.get(f"/export/estimate/{sq_id}/excel")
    assert a.status_code == b.status_code == 200
    cells_a, cells_b = _cells(a.content), _cells(b.content)
    assert cells_a == cells_b
    assert sum(len(rows) for rows in cells_a.values()) > 20


def test_golden_mutations_identical(clients, golden, run_id):
    (pg_id, sq_id), _, _ = golden
    pg, sq = clients
    for client, estimate_id in ((pg, pg_id), (sq, sq_id)):
        assert client.put(f"/estimates/{estimate_id}",
                          json={"estimate_name": f"golden-{run_id}-renamed"}).status_code == 200
    a = pg.post(f"/estimates/{pg_id}/duplicate")
    b = sq.post(f"/estimates/{sq_id}/duplicate")
    assert a.status_code == b.status_code == 200
    assert unordered_line_items(body(a)) == unordered_line_items(body(b))
    full_a = pg.get(f"/estimates/{a.json()['estimate_id']}/full")
    full_b = sq.get(f"/estimates/{b.json()['estimate_id']}/full")
    assert body(full_a) == body(full_b)


def test_fractional_inputs_are_stored_like_postgres(clients, run_id):
    """Model types differ from column types (fmapi_quantity is Numeric(18,2) in the
    model but BIGINT in the table, lakebase_cu NUMERIC(5,1)).
    PostgreSQL rounds on write; SQLite must round the same way."""
    pg, sq = clients
    item = dict(workload_type="FMAPI_DATABRICKS", fmapi_model="bge-large",
                fmapi_rate_type="input_token", fmapi_quantity=2_500_000.5,
                hours_per_month=10, vector_capacity_millions=2,
                lakebase_cu=0.55)
    results = []
    for client in (pg, sq):
        estimate = client.post("/estimates/", json=dict(
            estimate_name=f"fractional-{run_id}", cloud="aws", region="us-east-1",
            tier="PREMIUM")).json()["estimate_id"]
        created = client.post("/line-items/", json=dict(item, estimate_id=estimate,
                                                        workload_name="fractional"))
        assert created.status_code == 201, created.text
        results.append(body(client.get(f"/line-items/estimate/{estimate}")))
    assert results[0] == results[1]
