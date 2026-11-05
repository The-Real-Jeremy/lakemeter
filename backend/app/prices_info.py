"""As-of dates of the price data the app loads (for the UI and the API)."""
from __future__ import annotations

import csv
import json
from functools import lru_cache
from pathlib import Path

_BACKEND = Path(__file__).resolve().parents[1]


def _latest_fetched_at(paths: list[Path]) -> str | None:
    latest = None
    for path in paths:
        with path.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                value = (row.get("fetched_at") or "")[:10]
                if value and (latest is None or value > latest):
                    latest = value
    return latest


@lru_cache(maxsize=1)
def prices_info() -> dict:
    pricing = _BACKEND / "static" / "pricing"
    snapshot = {
        "dbu_as_of": _latest_fetched_at([pricing / "dbu-rates.csv"]),
        "vm_as_of": _latest_fetched_at(sorted(pricing.glob("vm-costs*.csv"))),
        "source": "upstream lakemeter-oss price snapshot (all clouds and regions)",
    }
    manifest_path = _BACKEND / "prices" / "PRICES.json"
    refreshed = json.loads(manifest_path.read_text()) if manifest_path.exists() else None
    return {"currency": "USD", "basis": "list", "refreshed": refreshed, "snapshot": snapshot}
