"""The Python pricing engine must return what the PL/pgSQL returns, digit for digit.

``lakemeter.calculate_line_item_costs`` runs on PostgreSQL; the same inputs go
through ``PricingEngine`` on the SQLite database. All 12 output columns are
compared as strings, so a scale difference (``1.5`` vs ``1.5000``) fails too.
Errors must match as well: if PostgreSQL raises, the engine must raise.

The grid is generated from the loaded pricing data so it covers every cloud,
every workload type the orchestrator prices, real instance/region/tier
combinations, NULL arguments, and rounding edge cases.
"""
from __future__ import annotations

import itertools
import random

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.services.lakebase_queries import call_calculate_line_item_costs
from app.services.pricing_engine import PricingEngine

FIELDS = (
    "dbu_per_hour", "hours_per_month", "dbu_per_month", "dbu_price",
    "dbu_cost_per_month", "driver_vm_cost_per_hour", "worker_vm_cost_per_hour",
    "total_vm_cost_per_hour", "driver_vm_cost_per_month",
    "total_worker_vm_cost_per_month", "vm_cost_per_month", "cost_per_month",
)

# hours_per_month goes through CAST(... AS INT): 100.5 and 101.5 probe the
# float rounding rule, 0.3 the truncation to zero.
USAGES = [
    dict(p13=3, p14=7, p15=30),
    dict(p13=1, p14=45, p15=22),
    dict(p13=None, p14=None, p15=None),
    dict(p13=24, p14=60, p15=31),
    dict(p16=100.5),
    dict(p16=101.5),
    dict(p16=730),
    dict(p16=0.3),
]


def params(**kwargs):
    base = {f"p{i}": None for i in range(1, 36)}
    base.update(kwargs)
    return base


def _rows(engine, sql):
    with engine.connect() as conn:
        return [tuple(r) for r in conn.execute(text(sql))]


def build_cases(engine) -> list[dict]:
    rng = random.Random(20261105)
    cases: list[dict] = []

    # Clouds/regions/tiers that actually have DBU prices, plus one that does not.
    locations = _rows(engine, """
        SELECT DISTINCT cloud, region, tier FROM lakemeter.sync_pricing_dbu_rates
        ORDER BY cloud, region, tier""")
    by_cloud: dict[str, list] = {}
    for cloud, region, tier in locations:
        by_cloud.setdefault(cloud, []).append((cloud, region, tier))
    locations = [loc for _, locs in sorted(by_cloud.items())
                 for loc in rng.sample(locs, min(3, len(locs)))]
    locations.append(("AWS", "nowhere-1", "PREMIUM"))

    for cloud, region, tier in locations:
        vm_rows = _rows(engine, f"""
            SELECT instance_type, pricing_tier, payment_option
            FROM lakemeter.sync_pricing_vm_costs
            WHERE cloud = '{cloud}' AND region = '{region}'""")
        instances = sorted({r[0] for r in vm_rows}) or ["i3.xlarge"]
        pricing = sorted({(r[1], r[2]) for r in vm_rows}) or [("on_demand", "NA")]
        nodes = rng.sample(instances, min(4, len(instances))) + ["no-such-node"]
        tiers = rng.sample(pricing, min(3, len(pricing))) + [("spot", "NA")]
        loc = dict(p2=cloud, p3=region, p4=tier)

        for workload in ("JOBS", "ALL_PURPOSE", "DLT"):
            editions = [None, "CORE", "PRO", "ADVANCED"] if workload == "DLT" else [None]
            for edition, photon, (driver_tier, driver_pay), (worker_tier, worker_pay) in itertools.product(
                    editions, (False, True, None), tiers[:2], tiers):
                driver, worker = rng.choice(nodes), rng.choice(nodes)
                cases.append(params(
                    p1=workload, **loc, p5=False, p6=photon, p7=edition,
                    p8=driver, p9=worker, p10=rng.choice([0, 1, 3, 8]),
                    p11=driver_tier, p12=worker_tier, p33=driver_pay, p34=worker_pay,
                    **rng.choice(USAGES)))
            for mode, serverless in itertools.product(("standard", "performance", None), (True, None)):
                cases.append(params(
                    p1=workload, **loc, p5=serverless, p6=False, p7=rng.choice(editions),
                    p8=rng.choice(nodes), p9=rng.choice(nodes), p10=rng.choice([0, 2, 5]),
                    p17=mode, **rng.choice(USAGES)))

        sizes = [r[0] for r in _rows(engine, f"""
            SELECT DISTINCT warehouse_size FROM lakemeter.sync_product_dbsql_rates
            WHERE UPPER(cloud) = '{cloud.upper()}' ORDER BY 1""")] or ["Small"]
        for warehouse, size, clusters in itertools.product(
                ("classic", "PRO", "serverless", None), sizes[:6] + ["Huge"], (None, 1, 3)):
            vm_tier, vm_pay = rng.choice(tiers)
            cases.append(params(
                p1="DBSQL", **loc, p18=warehouse, p19=size, p20=clusters,
                p21=vm_tier, p35=vm_pay, **rng.choice(USAGES)))

        modes = [r[0] for r in _rows(engine, f"""
            SELECT DISTINCT size_or_model FROM lakemeter.sync_product_serverless_rates
            WHERE UPPER(cloud) = '{cloud.upper()}' AND product = 'vector_search'""")]
        for mode, capacity in itertools.product(modes + ["unknown", None], (0, 0.5, 2, 3, 64, 100.25, None)):
            cases.append(params(p1="VECTOR_SEARCH", **loc, p22=mode, p23=capacity,
                                **rng.choice(USAGES)))

        gpus = [r[0] for r in _rows(engine, f"""
            SELECT DISTINCT size_or_model FROM lakemeter.sync_product_serverless_rates
            WHERE UPPER(cloud) = '{cloud.upper()}' AND product = 'model_serving'""")]
        for gpu in gpus + ["CPU", "gpu_nonexistent", None]:
            cases.append(params(p1="MODEL_SERVING", **loc, p24=gpu, **rng.choice(USAGES)))

        for cu, ha in itertools.product((None, 0, 1, 4, 32), (None, 1, 2, 3)):
            cases.append(params(p1="LAKEBASE", **loc, p31=cu, p32=ha, **rng.choice(USAGES)))

        for workload in ("FMAPI_DATABRICKS", "FMAPI_PROPRIETARY"):
            cases.append(params(p1=workload, **loc, p25="any", p26="anthropic",
                                p29="input_token", p30=1_000_000))

        for workload in ("DATABRICKS_APPS", "ZEROBUS", "AI_PARSE", "jobs", "NOT_A_TYPE", None):
            cases.append(params(p1=workload, **loc, **rng.choice(USAGES)))
    return cases


def _call_pg(session, case):
    try:
        row = call_calculate_line_item_costs(session, case)
        return ("ok", tuple(str(getattr(row, f)) for f in FIELDS))
    except Exception as exc:  # noqa: BLE001 - the error itself is compared
        session.rollback()
        return ("error", type(exc).__name__)


def _call_engine(session, case):
    try:
        row = PricingEngine(session).calculate_line_item_costs(case)
        return ("ok", tuple(str(getattr(row, f)) for f in FIELDS))
    except Exception as exc:  # noqa: BLE001
        return ("error", type(exc).__name__)


@pytest.fixture(scope="module")
def cases(sqlite_engine):
    generated = build_cases(sqlite_engine)
    assert len(generated) > 1000, "grid unexpectedly small; generator is broken"
    return generated


def test_engine_matches_plpgsql_to_the_digit(pg_engine, sqlite_engine, cases):
    by_type: dict[str, int] = {}
    errors = 0
    mismatches = []
    with Session(pg_engine) as pg, Session(sqlite_engine) as sq:
        for case in cases:
            expected = _call_pg(pg, case)
            actual = _call_engine(sq, case)
            by_type[str(case["p1"])] = by_type.get(str(case["p1"]), 0) + 1
            if expected[0] == "error":
                errors += 1
                if actual[0] != "error":
                    mismatches.append((case, expected, actual))
                continue
            if actual != expected:
                mismatches.append((case, expected, actual))
    print(f"\nengine parity: {len(cases)} cases, {errors} raise on both, by type {by_type}")
    assert not mismatches, (
        f"{len(mismatches)} of {len(cases)} differ; first:\n"
        + "\n".join(f"{c}\n  pg={e}\n  py={a}" for c, e, a in mismatches[:3]))


def test_grid_reaches_costs_not_just_zeros(sqlite_engine, cases):
    """Guard against a grid that 'passes' because every lookup misses."""
    with Session(sqlite_engine) as sq:
        priced = vm_priced = 0
        for case in cases:
            try:
                row = PricingEngine(sq).calculate_line_item_costs(case)
            except Exception:  # noqa: BLE001
                continue
            priced += (row.dbu_cost_per_month or 0) > 0
            vm_priced += (row.vm_cost_per_month or 0) > 0
    print(f"\ncoverage: {priced}/{len(cases)} priced a DBU cost, {vm_priced} a VM cost")
    assert priced > len(cases) * 0.4, f"only {priced} cases priced any DBU cost"
    assert vm_priced > 200, f"only {vm_priced} cases priced any VM cost"


@pytest.mark.parametrize("args", [
    ("JOBS", True, False, None, None, None),
    ("JOBS", None, True, None, None, None),
    ("DLT", False, True, "pro", None, None),
    ("DLT", None, None, None, None, None),
    ("DBSQL", False, False, None, "serverless", None),
    ("DBSQL", False, False, None, None, None),
    ("FMAPI_PROPRIETARY", False, False, None, None, "google"),
    ("FMAPI_PROPRIETARY", False, False, None, None, "openai"),
    ("FMAPI_PROPRIETARY", False, False, None, None, None),
    ("lakeflow_connect", False, False, None, None, None),
])
def test_product_type_matches(pg_engine, args):
    with pg_engine.connect() as conn:
        expected = conn.execute(text(
            "SELECT lakemeter.get_product_type_for_pricing(:a, :b, :c, :d, :e, :f)"),
            dict(zip("abcdef", args))).scalar()
    assert PricingEngine.get_product_type_for_pricing(*args) == expected


def test_unsupported_product_type_raises_on_both(pg_engine):
    with pytest.raises(Exception):
        with pg_engine.connect() as conn:
            conn.execute(text("SELECT lakemeter.get_product_type_for_pricing('NOPE')"))
    with pytest.raises(Exception):
        PricingEngine.get_product_type_for_pricing("NOPE")
