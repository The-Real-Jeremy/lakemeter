"""Python port of the lakemeter pricing functions in ``bootstrap/sql/functions.sql``.

PostgreSQL runs those functions as PL/pgSQL. SQLite has no stored functions,
so the SQLite backend calls this module instead. Each function mirrors its
PL/pgSQL original statement by statement, including:

* which declared variables round (``DECIMAL(18,4)`` / ``(10,4)`` / ``(18,2)``),
* NULL handling (an explicit ``None`` argument is NULL, not the default),
* case-sensitive ``CASE`` / ``IN`` matches where the original does not
  upper-case, and the lookups' ``LIMIT 1`` (first row in insertion order),
* the errors the original raises.

The lookups are plain SELECTs that run unchanged on both backends, so this
module can also be pointed at PostgreSQL; the parity test does exactly that.
See ``pg_numeric.py`` for the arithmetic rules.
"""
from __future__ import annotations

from collections import namedtuple
from decimal import Decimal
from typing import Any, Callable, Mapping, Optional

from sqlalchemy import text

from app.services.pg_numeric import (
    PgError, add, cast_bool, cast_int, cast_numeric, cast_varchar, ceiling,
    coalesce, div, lower, mul, num, sql_and, sql_not, typmod, upper,
)

ZERO = Decimal(0)

LineItemCosts = namedtuple("LineItemCosts", [
    "dbu_per_hour", "hours_per_month", "dbu_per_month", "dbu_price",
    "dbu_cost_per_month", "driver_vm_cost_per_hour", "worker_vm_cost_per_hour",
    "total_vm_cost_per_hour", "driver_vm_cost_per_month",
    "total_worker_vm_cost_per_month", "vm_cost_per_month", "cost_per_month",
])

VmCosts = namedtuple("VmCosts", [
    "driver_vm_cost_per_hour", "worker_vm_cost_per_hour", "total_vm_cost_per_hour",
    "driver_vm_cost_per_month", "total_worker_vm_cost_per_month",
    "total_vm_cost_per_month",
])


class PricingEngine:
    """Runs the pricing functions against a SQLAlchemy Session or Connection."""

    def __init__(self, db: Any):
        self._db = db

    def _first(self, sql: str, **params: Any) -> Optional[tuple]:
        row = self._db.execute(text(sql), params).fetchone()
        return tuple(row) if row is not None else None

    def _scalar(self, sql: str, **params: Any) -> Any:
        row = self._first(sql, **params)
        return None if row is None else row[0]

    # -- 01_Utility_Functions ------------------------------------------------

    @staticmethod
    def calculate_hours_per_month(
        workload_type, runs_per_day, avg_runtime_minutes, days_per_month,
        fmapi_rate_type=None, hours_per_month=None,
    ) -> Decimal:
        if hours_per_month is not None:
            return typmod(num(hours_per_month), 4)
        days = num(coalesce(days_per_month, 30))
        runs = num(coalesce(runs_per_day, 0))
        minutes = num(coalesce(avg_runtime_minutes, 0))
        usage = mul(mul(runs, div(minutes, Decimal("60.0"))), days)
        if workload_type in ("VECTOR_SEARCH", "MODEL_SERVING", "LAKEBASE"):
            hours = mul(Decimal("24.0"), days)
        elif (workload_type in ("FMAPI_DATABRICKS", "FMAPI_PROPRIETARY")
              and coalesce(fmapi_rate_type, "input_token") == "batch_inference"):
            hours = usage
        elif workload_type in ("FMAPI_DATABRICKS", "FMAPI_PROPRIETARY"):
            hours = ZERO
        else:
            hours = usage
        return typmod(hours, 4)

    @staticmethod
    def get_product_type_for_pricing(
        workload_type, serverless_enabled=False, photon_enabled=False,
        dlt_edition=None, dbsql_warehouse_type=None, fmapi_provider=None,
    ) -> Optional[str]:
        wt = upper(workload_type)
        dlt = upper(dlt_edition)
        dbsql = upper(dbsql_warehouse_type)
        provider = upper(fmapi_provider)
        serverless = serverless_enabled is True
        photon = photon_enabled is True

        if wt == "JOBS":
            if serverless:
                return "JOBS_SERVERLESS_COMPUTE"
            return "JOBS_COMPUTE_(PHOTON)" if photon else "JOBS_COMPUTE"
        if wt == "ALL_PURPOSE":
            if serverless:
                return "ALL_PURPOSE_SERVERLESS_COMPUTE"
            return "ALL_PURPOSE_COMPUTE_(PHOTON)" if photon else "ALL_PURPOSE_COMPUTE"
        if wt == "DLT":
            if serverless:
                return "JOBS_SERVERLESS_COMPUTE"
            product = "DLT_" + coalesce(dlt, "CORE") + "_COMPUTE"
            return product + "_(PHOTON)" if photon else product
        if wt == "DBSQL":
            if dbsql == "SERVERLESS":
                return "SERVERLESS_SQL_COMPUTE"
            if dbsql == "PRO":
                return "SQL_PRO_COMPUTE"
            return "SQL_COMPUTE"
        if wt in ("VECTOR_SEARCH", "MODEL_SERVING", "FMAPI_DATABRICKS",
                  "AI_PARSE", "AI_EXTRACT", "AI_CLASSIFY", "AI_GATEWAY",
                  "AGENT_EVALUATION", "SHUTTERSTOCK_IMAGEAI"):
            return "SERVERLESS_REAL_TIME_INFERENCE"
        if wt == "FMAPI_PROPRIETARY":
            if provider == "GOOGLE":
                return "GEMINI_MODEL_SERVING"
            # text || NULL is NULL, as in the original.
            return None if provider is None else provider + "_MODEL_SERVING"
        if wt == "LAKEBASE":
            return "DATABASE_SERVERLESS_COMPUTE"
        if wt == "DATABRICKS_APPS":
            return "ALL_PURPOSE_SERVERLESS_COMPUTE"
        if wt == "AI_RUNTIME":
            return "MODEL_TRAINING"
        if wt == "GENERAL_STORAGE":
            return "DATABRICKS_STORAGE"
        if wt in ("ZEROBUS", "LAKEFLOW_CONNECT"):
            return "JOBS_SERVERLESS_COMPUTE"
        raise PgError(f"Unsupported workload type: {workload_type}")

    def get_dbu_price(self, cloud, region, tier, product_type) -> Decimal:
        price = self._scalar(
            """
            SELECT price_per_dbu
            FROM lakemeter.sync_pricing_dbu_rates
            WHERE UPPER(cloud) = UPPER(:cloud)
              AND UPPER(region) = UPPER(:region)
              AND UPPER(tier) = UPPER(:tier)
              AND (UPPER(sku_name) = UPPER(:product_type)
                   OR UPPER(COALESCE(product_type, '')) = UPPER(:product_type))
            LIMIT 1
            """,
            cloud=cloud, region=region, tier=tier, product_type=product_type,
        )
        return coalesce(typmod(num(price), 4), ZERO)

    def get_photon_multiplier(
        self, cloud, workload_type, dlt_edition=None,
        photon_enabled=False, serverless_enabled=False,
    ) -> Decimal:
        if sql_and(sql_not(photon_enabled), sql_not(serverless_enabled)) is True:
            return Decimal("1.0")
        if workload_type == "DLT":
            sku_type = "DLT_" + upper(coalesce(dlt_edition, "CORE")) + "_COMPUTE"
        elif workload_type == "ALL_PURPOSE":
            sku_type = "ALL_PURPOSE_COMPUTE"
        else:
            sku_type = "JOBS_COMPUTE"
        multiplier = self._scalar(
            """
            SELECT multiplier
            FROM lakemeter.sync_ref_dbu_multipliers
            WHERE UPPER(cloud) = UPPER(:cloud)
              AND UPPER(sku_type) = UPPER(:sku_type)
              AND feature = 'photon'
            LIMIT 1
            """,
            cloud=cloud, sku_type=sku_type,
        )
        return coalesce(typmod(num(multiplier), 4), Decimal("1.0"))

    # -- 02/03 classic and serverless compute --------------------------------

    def _instance_dbu(self, cloud, instance_type) -> Optional[Decimal]:
        rate = self._scalar(
            """
            SELECT dbu_rate
            FROM lakemeter.sync_ref_instance_dbu_rates
            WHERE UPPER(cloud) = UPPER(:cloud)
              AND UPPER(instance_type) = UPPER(:instance_type)
            LIMIT 1
            """,
            cloud=cloud, instance_type=instance_type,
        )
        return typmod(num(rate), 4)

    def _base_dbu(self, cloud, driver, worker, num_workers) -> Optional[Decimal]:
        driver_dbu = self._instance_dbu(cloud, driver)
        worker_dbu = self._instance_dbu(cloud, worker)
        return add(
            coalesce(driver_dbu, ZERO),
            mul(coalesce(worker_dbu, ZERO), num(coalesce(num_workers, 0))),
        )

    def calculate_classic_compute_dbu(
        self, cloud, driver_node_type, worker_node_type, num_workers,
        photon_enabled, workload_type, dlt_edition=None,
    ) -> Optional[Decimal]:
        base = self._base_dbu(cloud, driver_node_type, worker_node_type, num_workers)
        photon = typmod(self.get_photon_multiplier(
            cloud, workload_type, dlt_edition, photon_enabled, False), 4)
        return typmod(mul(base, photon), 4)

    def calculate_serverless_compute_dbu(
        self, cloud, driver_node_type, worker_node_type, num_workers,
        workload_type="JOBS", serverless_mode="standard",
    ) -> Optional[Decimal]:
        base = self._base_dbu(cloud, driver_node_type, worker_node_type, num_workers)
        photon = typmod(self.get_photon_multiplier(
            cloud, workload_type, None, True, True), 4)
        if upper(coalesce(workload_type, "")) == "ALL_PURPOSE":
            mode = Decimal("2.0")
        elif lower(coalesce(serverless_mode, "standard")) == "performance":
            mode = Decimal("2.0")
        else:
            mode = Decimal("1.0")
        return typmod(mul(mul(base, photon), typmod(mode, 4)), 4)

    # -- 04 DBSQL ------------------------------------------------------------

    def calculate_dbsql_dbu(
        self, cloud, warehouse_type, warehouse_size, num_clusters=1,
    ) -> Optional[Decimal]:
        rate = self._scalar(
            """
            SELECT dbu_per_hour
            FROM lakemeter.sync_product_dbsql_rates
            WHERE UPPER(cloud) = UPPER(:cloud)
              AND warehouse_type = LOWER(:warehouse_type)
              AND UPPER(warehouse_size) = UPPER(:warehouse_size)
            LIMIT 1
            """,
            cloud=cloud, warehouse_type=warehouse_type, warehouse_size=warehouse_size,
        )
        rate = typmod(num(rate), 4)
        return typmod(mul(coalesce(rate, ZERO), num(coalesce(num_clusters, 1))), 4)

    # -- 05 AI Search and Model Serving --------------------------------------

    def _serverless_rate(self, cloud, product, size_or_model) -> Optional[Decimal]:
        rate = self._scalar(
            """
            SELECT dbu_rate
            FROM lakemeter.sync_product_serverless_rates
            WHERE UPPER(cloud) = UPPER(:cloud)
              AND product = :product
              AND UPPER(size_or_model) = UPPER(:size_or_model)
            LIMIT 1
            """,
            cloud=cloud, product=product, size_or_model=size_or_model,
        )
        return typmod(num(rate), 4)

    def calculate_vector_search_dbu(
        self, cloud, vector_search_mode, capacity_millions,
    ) -> Optional[Decimal]:
        rate = self._serverless_rate(cloud, "vector_search", vector_search_mode)
        if lower(vector_search_mode) == "storage_optimized":
            divisor = typmod(Decimal("64.0"), 4)
        else:
            divisor = typmod(Decimal("2.0"), 4)
        units = typmod(ceiling(div(num(capacity_millions), divisor)), 4)
        return typmod(mul(coalesce(rate, ZERO), units), 4)

    def calculate_model_serving_dbu(
        self, cloud, serverless_size, concurrency=4,
    ) -> Optional[Decimal]:
        rate = self._serverless_rate(cloud, "model_serving", serverless_size)
        replicas = num(coalesce(concurrency, 4))
        if not lower(coalesce(serverless_size, "cpu")).startswith("cpu"):
            replicas = div(replicas, Decimal("4.0"))
        # The declared return type does not round; the caller's variable does.
        return mul(coalesce(rate, ZERO), replicas)

    # -- 06 FMAPI --------------------------------------------------------------

    @staticmethod
    def calculate_fmapi_databricks_dbu(*_args, **_kwargs):
        # Upstream defect, mirrored on purpose: the PL/pgSQL body uses
        # COALESCE(input_divisor, 1) on a TEXT column, so PostgreSQL rejects
        # the function every time it runs. No route calls it (the FMAPI
        # routes price in Python), and the parity test asserts both backends
        # fail the same way.
        raise PgError("COALESCE types text and integer cannot be matched")

    calculate_fmapi_proprietary_dbu = calculate_fmapi_databricks_dbu

    @staticmethod
    def calculate_lakebase_dbu(lakebase_cu=0, ha_nodes=1) -> Decimal:
        product = cast_int(coalesce(lakebase_cu, 0)) * cast_int(coalesce(ha_nodes, 1))
        return typmod(num(product), 4)

    # -- 08 VM costs -----------------------------------------------------------

    def _vm_cost(self, cloud, region, instance_type, pricing_tier,
                 payment_option, exclude_spot: bool) -> Optional[Decimal]:
        spot_clause = "AND UPPER(pricing_tier) != 'SPOT'" if exclude_spot else ""
        cost = self._scalar(
            f"""
            SELECT cost_per_hour
            FROM lakemeter.sync_pricing_vm_costs
            WHERE UPPER(cloud) = UPPER(:cloud)
              AND UPPER(region) = UPPER(:region)
              AND UPPER(instance_type) = UPPER(:instance_type)
              AND UPPER(pricing_tier) = UPPER(:pricing_tier)
              AND UPPER(payment_option) = UPPER(COALESCE(:payment_option, 'NA'))
              {spot_clause}
            LIMIT 1
            """,
            cloud=cloud, region=region, instance_type=instance_type,
            pricing_tier=pricing_tier, payment_option=payment_option,
        )
        return typmod(num(cost), 4)

    @staticmethod
    def _vm_totals(driver_per_hour, worker_per_hour, hours) -> VmCosts:
        total_per_hour = typmod(add(driver_per_hour, worker_per_hour), 4)
        hours = num(coalesce(hours, 0))
        return VmCosts(
            driver_per_hour,
            worker_per_hour,
            total_per_hour,
            typmod(mul(driver_per_hour, hours), 4),
            typmod(mul(worker_per_hour, hours), 4),
            typmod(mul(total_per_hour, hours), 4),
        )

    def calculate_classic_vm_costs(
        self, cloud, region, driver_node_type, worker_node_type, num_workers,
        driver_pricing_tier, worker_pricing_tier, hours_per_month,
        driver_payment_option="NA", worker_payment_option="NA",
    ) -> VmCosts:
        driver = self._vm_cost(cloud, region, driver_node_type, driver_pricing_tier,
                               driver_payment_option, exclude_spot=True)
        worker = self._vm_cost(cloud, region, worker_node_type, worker_pricing_tier,
                               worker_payment_option, exclude_spot=False)
        driver = typmod(coalesce(driver, ZERO), 4)
        worker = typmod(mul(coalesce(worker, ZERO), num(coalesce(num_workers, 0))), 4)
        return self._vm_totals(driver, worker, hours_per_month)

    def calculate_dbsql_vm_costs(
        self, cloud, region, warehouse_type, warehouse_size, num_clusters,
        vm_pricing_tier, hours_per_month, vm_payment_option="NA",
    ) -> VmCosts:
        config = self._first(
            """
            SELECT driver_instance_type, worker_instance_type, worker_count
            FROM lakemeter.sync_ref_dbsql_warehouse_config
            WHERE UPPER(cloud) = UPPER(:cloud)
              AND LOWER(warehouse_type) = LOWER(:warehouse_type)
              AND UPPER(warehouse_size) = UPPER(:warehouse_size)
            LIMIT 1
            """,
            cloud=cloud, warehouse_type=lower(warehouse_type),
            warehouse_size=warehouse_size,
        )
        driver_instance, worker_instance, worker_count = config or (None, None, None)
        num_workers = cast_int(worker_count)  # TEXT -> INT variable
        driver = self._vm_cost(cloud, region, driver_instance, vm_pricing_tier,
                               vm_payment_option, exclude_spot=True)
        worker = self._vm_cost(cloud, region, worker_instance, vm_pricing_tier,
                               vm_payment_option, exclude_spot=False)
        clusters = num(coalesce(num_clusters, 1))
        driver = typmod(mul(coalesce(driver, ZERO), clusters), 4)
        worker = typmod(mul(mul(coalesce(worker, ZERO), num(coalesce(num_workers, 0))),
                            clusters), 4)
        return self._vm_totals(driver, worker, hours_per_month)

    # -- 09 orchestrator -------------------------------------------------------

    def calculate_line_item_costs(self, params: Mapping[str, Any]) -> LineItemCosts:
        """``lakemeter.calculate_line_item_costs`` called with p1..p35 (+ optional p36).

        The CASTs mirror ``lakebase_queries.call_calculate_line_item_costs``.
        """
        p = {k: params.get(k) for k in (f"p{i}" for i in range(1, 37))}
        workload_type = cast_varchar(p["p1"])
        cloud = cast_varchar(p["p2"])
        region = cast_varchar(p["p3"])
        tier = cast_varchar(p["p4"])
        serverless = cast_bool(p["p5"])
        photon = cast_bool(p["p6"])
        dlt_edition = cast_varchar(p["p7"])
        driver_node = cast_varchar(p["p8"])
        worker_node = cast_varchar(p["p9"])
        num_workers = cast_int(p["p10"])
        driver_tier = cast_varchar(p["p11"])
        worker_tier = cast_varchar(p["p12"])
        runs_per_day = cast_int(p["p13"])
        avg_runtime = cast_int(p["p14"])
        days_per_month = cast_int(p["p15"])
        hours_override = cast_int(p["p16"])
        serverless_mode = cast_varchar(p["p17"])
        dbsql_type = cast_varchar(p["p18"])
        dbsql_size = cast_varchar(p["p19"])
        dbsql_clusters = cast_int(p["p20"])
        dbsql_vm_tier = cast_varchar(p["p21"])
        vector_mode = cast_varchar(p["p22"])
        vector_capacity = cast_numeric(p["p23"])
        gpu_type = cast_varchar(p["p24"])
        fmapi_model = cast_varchar(p["p25"])
        fmapi_provider = cast_varchar(p["p26"])
        fmapi_rate_type = cast_varchar(p["p29"])
        lakebase_cu = cast_int(p["p31"])
        lakebase_ha = cast_int(p["p32"])
        driver_payment = cast_varchar(p["p33"])
        worker_payment = cast_varchar(p["p34"])
        dbsql_payment = cast_varchar(p["p35"])
        concurrency = cast_int(p["p36"]) if "p36" in params else 4

        dbu_per_hour = typmod(ZERO, 4)
        dbu_per_month = typmod(ZERO, 4)
        driver_vm_hour = worker_vm_hour = total_vm_hour = typmod(ZERO, 4)
        driver_vm_month = worker_vm_month = typmod(ZERO, 4)
        vm_month = typmod(ZERO, 2)

        hours = typmod(self.calculate_hours_per_month(
            workload_type, runs_per_day, avg_runtime, days_per_month,
            fmapi_rate_type, hours_override), 4)
        product_type = self.get_product_type_for_pricing(
            workload_type, serverless, photon, dlt_edition, dbsql_type, fmapi_provider)

        vm = None
        if workload_type in ("JOBS", "ALL_PURPOSE", "DLT"):
            if sql_not(serverless) is True:
                dbu_per_hour = typmod(self.calculate_classic_compute_dbu(
                    cloud, driver_node, worker_node, num_workers, photon,
                    workload_type, dlt_edition), 4)
                dbu_per_month = typmod(mul(dbu_per_hour, hours), 4)
                vm = self.calculate_classic_vm_costs(
                    cloud, region, driver_node, worker_node, num_workers,
                    driver_tier, worker_tier, hours, driver_payment, worker_payment)
            else:
                dbu_per_hour = typmod(self.calculate_serverless_compute_dbu(
                    cloud, driver_node, worker_node, num_workers,
                    workload_type, serverless_mode), 4)
                dbu_per_month = typmod(mul(dbu_per_hour, hours), 4)
        elif workload_type == "DBSQL":
            dbu_per_hour = typmod(self.calculate_dbsql_dbu(
                cloud, dbsql_type, dbsql_size, dbsql_clusters), 4)
            dbu_per_month = typmod(mul(dbu_per_hour, hours), 4)
            if lower(dbsql_type) in ("classic", "pro"):
                vm = self.calculate_dbsql_vm_costs(
                    cloud, region, dbsql_type, dbsql_size, dbsql_clusters,
                    dbsql_vm_tier, hours, dbsql_payment)
        elif workload_type == "VECTOR_SEARCH":
            dbu_per_hour = typmod(self.calculate_vector_search_dbu(
                cloud, vector_mode, vector_capacity), 4)
            dbu_per_month = typmod(mul(dbu_per_hour, hours), 4)
        elif workload_type == "MODEL_SERVING":
            dbu_per_hour = typmod(self.calculate_model_serving_dbu(
                cloud, gpu_type, concurrency), 4)
            dbu_per_month = typmod(mul(dbu_per_hour, hours), 4)
        elif workload_type in ("FMAPI_DATABRICKS", "FMAPI_PROPRIETARY"):
            self.calculate_fmapi_databricks_dbu(cloud, fmapi_model)
        elif workload_type == "LAKEBASE":
            dbu_per_hour = typmod(self.calculate_lakebase_dbu(lakebase_cu, lakebase_ha), 4)
            dbu_per_month = typmod(mul(dbu_per_hour, hours), 4)

        if vm is not None:
            driver_vm_hour = typmod(vm.driver_vm_cost_per_hour, 4)
            worker_vm_hour = typmod(vm.worker_vm_cost_per_hour, 4)
            total_vm_hour = typmod(vm.total_vm_cost_per_hour, 4)
            driver_vm_month = typmod(vm.driver_vm_cost_per_month, 4)
            worker_vm_month = typmod(vm.total_worker_vm_cost_per_month, 4)
            vm_month = typmod(vm.total_vm_cost_per_month, 2)

        dbu_price = typmod(self.get_dbu_price(cloud, region, tier, product_type), 4)
        dbu_cost = typmod(mul(dbu_per_month, dbu_price), 2)
        cost = typmod(add(dbu_cost, vm_month), 2)
        return LineItemCosts(
            dbu_per_hour, hours, dbu_per_month, dbu_price, dbu_cost,
            driver_vm_hour, worker_vm_hour, total_vm_hour,
            driver_vm_month, worker_vm_month, vm_month, cost,
        )


def is_sqlite(db: Any) -> bool:
    """Whether a Session/Connection is bound to the SQLite backend."""
    bind: Callable | Any = getattr(db, "get_bind", None)
    engine = bind() if callable(bind) else getattr(db, "engine", None)
    return getattr(getattr(engine, "dialect", None), "name", "") == "sqlite"
