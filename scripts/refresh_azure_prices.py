#!/usr/bin/env python3
"""Refresh Azure list prices for chosen regions into backend/prices/.

    python3 scripts/refresh_azure_prices.py australiaeast australiasoutheast

Source: the public Azure Retail Prices API (https://prices.azure.com/api/retail/prices,
no authentication). Writes, per region:

  backend/prices/azure-<region>-dbu-rates.csv   Databricks DBU list prices, USD
  backend/prices/azure-<region>-vm-costs.csv    Linux VM prices, USD (on demand, spot, 1y/3y reserved)
  backend/prices/reference/azure-<region>-dbu-rates-AUD.csv   the same DBU prices in AUD (not loaded)

and backend/prices/PRICES.json (as-of date, sources, counts). At start-up the app loads
the bundled upstream snapshot (backend/static/pricing) and then these files on top: a
row here replaces the snapshot row with the same key, anything else is left as shipped.

Prices the Retail API does not publish, or publishes wrongly, are set in
backend/prices/overrides.csv with their source; that file is never regenerated.
Commit the result: the prices are part of the repo.
"""
from __future__ import annotations

import csv
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "backend" / "prices"
SNAPSHOT = ROOT / "backend" / "static" / "pricing"
API = "https://prices.azure.com/api/retail/prices"

# Azure Retail "skuName" (Premium tier) -> Lakemeter sku_name.
DBU_METERS = {
    "Premium Jobs Compute": "JOBS_COMPUTE",
    "Premium Jobs Compute Photon": "JOBS_COMPUTE_(PHOTON)",
    "Premium Jobs Light Compute": "JOBS_LIGHT_COMPUTE",
    "Premium All-purpose Compute": "ALL_PURPOSE_COMPUTE",
    "Premium All-Purpose Photon": "ALL_PURPOSE_COMPUTE_(PHOTON)",
    "Premium Core Compute Delta Live Tables": "DLT_CORE_COMPUTE",
    "Premium Core Compute Photon Delta Live Tables": "DLT_CORE_COMPUTE_(PHOTON)",
    "Premium Pro Compute Delta Live Tables": "DLT_PRO_COMPUTE",
    "Premium Pro Compute Photon Delta Live Tables": "DLT_PRO_COMPUTE_(PHOTON)",
    "Premium Advanced Compute Delta Live Tables": "DLT_ADVANCED_COMPUTE",
    "Premium Advanced Compute Photon Delta Live Tables": "DLT_ADVANCED_COMPUTE_(PHOTON)",
    "Premium Automated Serverless Compute": "JOBS_SERVERLESS_COMPUTE",
    "Premium Interactive Serverless Compute": "ALL_PURPOSE_SERVERLESS_COMPUTE",
    "Premium SQL Analytics": "SQL_COMPUTE",
    "Premium SQL Compute Pro": "SQL_PRO_COMPUTE",
    "Premium Serverless SQL": "SERVERLESS_SQL_COMPUTE",
    "Premium Serverless Realtime Inferencing": "SERVERLESS_REAL_TIME_INFERENCE",
    "Launch Charge Serverless Realtime Inferencing": "SERVERLESS_REAL_TIME_INFERENCE_LAUNCH",
    "Premium Model Training": "MODEL_TRAINING",
    "Premium Database Serverless Compute": "DATABASE_SERVERLESS_COMPUTE",
    "Premium Databricks Storage Unit": "DATABRICKS_STORAGE",
}
DBU_COLUMNS = ["sku_name", "cloud", "tier", "product_type", "sku_region", "region", "usage_unit",
               "price_per_dbu", "currency_code", "pricing_type", "fetched_at"]
VM_COLUMNS = ["cloud", "region", "instance_type", "pricing_tier", "payment_option",
              "cost_per_hour", "currency", "source", "fetched_at"]


def fetch(flt: str, currency: str = "USD") -> list[dict]:
    url = API + "?" + urllib.parse.urlencode({"currencyCode": currency, "$filter": flt})
    items: list[dict] = []
    while url:
        for attempt in range(6):
            try:
                with urllib.request.urlopen(url, timeout=90) as response:
                    page = json.load(response)
                break
            except Exception:  # noqa: BLE001 - retry transient API errors
                if attempt == 5:
                    raise
                time.sleep(2 ** attempt)
        items += page.get("Items", [])
        url = page.get("NextPageLink")
    return items


def dbu_rows(region: str, currency: str, now: str) -> tuple[list[dict], list[str]]:
    items = fetch(f"serviceName eq 'Azure Databricks' and armRegionName eq '{region}' "
                  "and type eq 'Consumption'", currency)
    rows, seen = [], {}
    for item in items:
        sku = DBU_METERS.get(item["skuName"])
        if not sku:
            continue
        price = item["retailPrice"]
        if sku in seen and seen[sku] != price:
            raise SystemExit(f"{region}: two prices for {sku}: {seen[sku]} and {price}")
        seen[sku] = price
    for sku, price in sorted(seen.items()):
        rows.append(dict(sku_name=sku, cloud="AZURE", tier="PREMIUM", product_type="", sku_region="",
                         region=region, usage_unit="DBU", price_per_dbu=price,
                         currency_code=currency, pricing_type="", fetched_at=now))
    missing = sorted(set(DBU_METERS.values()) - set(seen))
    return rows, missing


def _variants(name: str) -> set[str]:
    lower = name.lower()
    bare = lower.replace("standard_", "")
    out = {lower, bare, "standard_" + bare}
    m = re.match(r"^d(\d+)s(_v\d+)?$", bare)
    if m:
        out |= {f"ds{m.group(1)}{m.group(2) or ''}", f"standard_ds{m.group(1)}{m.group(2) or ''}"}
    m = re.match(r"^ds(\d+)(_v\d+)?$", bare)
    if m:
        out |= {f"d{m.group(1)}s{m.group(2) or ''}", f"standard_d{m.group(1)}s{m.group(2) or ''}"}
    return out


def azure_instance_types() -> dict[str, str]:
    """Variant (lower case) -> the instance name Lakemeter uses."""
    lookup = {}
    with (SNAPSHOT / "instance-dbu-rates.csv").open() as handle:
        for row in csv.DictReader(handle):
            if row["cloud"].upper() == "AZURE":
                for variant in _variants(row["instance_type"]):
                    lookup.setdefault(variant, row["instance_type"])
    return lookup


def vm_rows(region: str, now: str) -> list[dict]:
    lookup = azure_instance_types()
    rows, seen = [], set()
    for price_type in ("Consumption", "Reservation"):
        items = fetch(f"serviceName eq 'Virtual Machines' and armRegionName eq '{region}' "
                      f"and priceType eq '{price_type}' and contains(productName, 'Windows') eq false")
        for item in items:
            name = next((lookup[v] for v in _variants(item.get("armSkuName") or "") if v in lookup), None)
            if not name:
                continue
            display = item.get("skuName", "")
            if "Low Priority" in display:
                continue
            if "Spot" in display:
                tier = "spot"
            elif item.get("reservationTerm") == "1 Year":
                tier = "reserved_1y"
            elif item.get("reservationTerm") == "3 Years":
                tier = "reserved_3y"
            else:
                tier = "on_demand"
            if (name, tier) in seen:
                continue
            seen.add((name, tier))
            cost = item["retailPrice"]
            if tier.startswith("reserved"):
                # A reservation's retailPrice is the WHOLE term's price, even though its
                # unitOfMeasure says "1 Hour" (1y D128ds_v6: 54135.0 -> 6.18/hour).
                cost /= 8760 * (3 if tier == "reserved_3y" else 1)
            elif "Month" in item.get("unitOfMeasure", ""):
                cost /= 730
            rows.append(dict(cloud="AZURE", region=region, instance_type=name, pricing_tier=tier,
                             payment_option="NA", cost_per_hour=round(cost, 6), currency="USD",
                             source="Azure Retail Prices API", fetched_at=now))
    return sorted(rows, key=lambda r: (r["instance_type"], r["pricing_tier"]))


def apply_overrides(region: str, rows: list[dict], now: str) -> list[str]:
    path = OUT / "overrides.csv"
    applied = []
    if not path.exists():
        return applied
    with path.open() as handle:
        for override in csv.DictReader(handle):
            if override["region"] not in (region, "*"):
                continue
            row = next((r for r in rows if r["sku_name"] == override["sku_name"]), None)
            if row is None:
                row = dict(sku_name=override["sku_name"], cloud="AZURE", tier="PREMIUM",
                           product_type="", sku_region="", region=region, usage_unit="DBU",
                           currency_code="USD", pricing_type="")
                rows.append(row)
            row["price_per_dbu"] = float(override["price_per_dbu"])
            row["fetched_at"] = now
            applied.append(f"{override['sku_name']}={override['price_per_dbu']} ({override['source']})")
    rows.sort(key=lambda r: r["sku_name"])
    return applied


def write(path: Path, columns: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def main(regions: list[str]) -> None:
    if not regions:
        raise SystemExit(__doc__)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    manifest = {"as_of": now[:10], "fetched_at": now, "currency": "USD",
                "source": "Azure Retail Prices API (prices.azure.com), public list prices",
                "regions": {}}
    for region in regions:
        dbu, missing = dbu_rows(region, "USD", now)
        overrides = apply_overrides(region, dbu, now)
        aud, _ = dbu_rows(region, "AUD", now)
        vm = vm_rows(region, now)
        if len(dbu) < 15 or len(vm) < 100:
            raise SystemExit(f"{region}: only {len(dbu)} DBU and {len(vm)} VM prices; refusing to write")
        write(OUT / f"azure-{region}-dbu-rates.csv", DBU_COLUMNS, dbu)
        write(OUT / f"azure-{region}-vm-costs.csv", VM_COLUMNS, vm)
        write(OUT / "reference" / f"azure-{region}-dbu-rates-AUD.csv", DBU_COLUMNS, aud)
        manifest["regions"][region] = {
            "dbu_prices": len(dbu), "vm_prices": len(vm),
            "vm_instance_types": len({r["instance_type"] for r in vm}),
            "not_in_retail_api": missing, "overrides": overrides}
        print(f"{region}: {len(dbu)} DBU, {len(vm)} VM prices; overrides {overrides}; "
              f"not in the Retail API {missing}")
    (OUT / "PRICES.json").write_text(json.dumps(manifest, indent=2) + "\n")


if __name__ == "__main__":
    main(sys.argv[1:])
