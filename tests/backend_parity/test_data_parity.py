"""Every table the bootstrap fills must hold the same rows on both backends.

The pricing tables are compared in insertion order, not as sets: the pricing
lookups use ``LIMIT 1`` without ``ORDER BY``, so where a key has duplicate
rows the FIRST one wins, on PostgreSQL by physical order and on SQLite by
rowid. Both are insertion order after TRUNCATE + load.
"""
import math

import pytest
from sqlalchemy import text

ORDERED = [
    "sync_pricing_dbu_rates",
    "sync_pricing_vm_costs",
    "sync_ref_instance_dbu_rates",
    "sync_ref_dbu_multipliers",
    "sync_product_dbsql_rates",
    "sync_ref_dbsql_warehouse_config",
    "sync_product_serverless_rates",
    "sync_product_fmapi_databricks",
    "sync_product_fmapi_proprietary",
    "sync_ref_sku_region_map",
]
# Filled by INSERT ... SELECT DISTINCT or upserts, so only the set is defined.
UNORDERED = [
    "ref_workload_types",
    "ref_cloud_tiers",
    "ref_model_serving_gpu_types",
    "ref_fmapi_databricks_models",
    "ref_fmapi_proprietary_models",
    "sku_discount_mapping",
]
VOLATILE = {"updated_at"}


def _columns(engine, table):
    with engine.connect() as conn:
        row = conn.execute(text(f"SELECT * FROM lakemeter.{table} LIMIT 1"))
        return [c for c in row.keys() if c not in VOLATILE]


def _normalise(value):
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, float) and math.isnan(value):
        return "NaN"
    return value


def _rows(engine, table, columns, order_by):
    sql = f"SELECT {', '.join(columns)} FROM lakemeter.{table} ORDER BY {order_by}"
    with engine.connect() as conn:
        return [tuple(_normalise(v) for v in row) for row in conn.execute(text(sql))]


@pytest.mark.parametrize("table", ORDERED)
def test_pricing_table_identical_in_insertion_order(pg_engine, sqlite_engine, table):
    columns = _columns(pg_engine, table)
    assert columns == _columns(sqlite_engine, table)
    pg_rows = _rows(pg_engine, table, columns, "ctid")
    sq_rows = _rows(sqlite_engine, table, columns, "rowid")
    assert len(pg_rows) == len(sq_rows) > 0
    mismatches = [(i, a, b) for i, (a, b) in enumerate(zip(pg_rows, sq_rows)) if a != b]
    assert not mismatches, f"{len(mismatches)} rows differ, first: {mismatches[:3]}"


@pytest.mark.parametrize("table", UNORDERED)
def test_reference_table_identical_as_a_set(pg_engine, sqlite_engine, table):
    columns = _columns(pg_engine, table)
    order = ", ".join(columns)
    pg_rows = sorted(_rows(pg_engine, table, columns, order), key=repr)
    sq_rows = sorted(_rows(sqlite_engine, table, columns, order), key=repr)
    assert pg_rows == sq_rows
    assert len(pg_rows) > 0
