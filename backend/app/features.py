"""Optional features that depend on the workspace, decided once and cached.

The AI assistant needs a Claude serving endpoint. Air-gapped workspaces often
have none (no Foundation Model APIs), so the assistant is switched off cleanly
instead of failing on every message.

``LAKEMETER_AI_ASSISTANT``:

* ``auto`` (default): on only if the configured endpoint exists and is READY.
* ``on``: always on (skip the check).
* ``off``: always off.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)

_CACHE_SECONDS = 600
_lock = threading.Lock()
_cached: Optional[tuple[float, dict]] = None


def _endpoint_name() -> str:
    return os.getenv("CLAUDE_MODEL_ENDPOINT", "databricks-claude-opus-4-6")


def _probe() -> dict:
    mode = os.getenv("LAKEMETER_AI_ASSISTANT", "auto").strip().lower()
    endpoint = _endpoint_name()
    if mode == "off":
        return {"enabled": False, "endpoint": endpoint,
                "reason": "switched off (LAKEMETER_AI_ASSISTANT=off)"}
    if mode == "on":
        return {"enabled": True, "endpoint": endpoint,
                "reason": "switched on (LAKEMETER_AI_ASSISTANT=on)"}
    if not os.getenv("DATABRICKS_HOST"):
        return {"enabled": False, "endpoint": endpoint,
                "reason": "not running in a Databricks workspace"}
    try:
        from databricks.sdk import WorkspaceClient

        state = WorkspaceClient().serving_endpoints.get(endpoint).state
        ready = getattr(getattr(state, "ready", None), "value", str(getattr(state, "ready", "")))
    except Exception as error:  # noqa: BLE001 - any failure means "not available"
        logger.warning("AI assistant disabled: endpoint %s unavailable: %s", endpoint, error)
        return {"enabled": False, "endpoint": endpoint,
                "reason": f"serving endpoint '{endpoint}' is not available in this workspace"}
    if ready != "READY":
        return {"enabled": False, "endpoint": endpoint,
                "reason": f"serving endpoint '{endpoint}' is not ready ({ready})"}
    return {"enabled": True, "endpoint": endpoint, "reason": "serving endpoint is ready"}


def ai_assistant_status(refresh: bool = False) -> dict:
    global _cached
    with _lock:
        if refresh or _cached is None or time.time() - _cached[0] > _CACHE_SECONDS:
            _cached = (time.time(), _probe())
        return dict(_cached[1])


def require_ai_assistant() -> None:
    """FastAPI dependency: a clean 503 instead of a failed model call."""
    status = ai_assistant_status()
    if not status["enabled"]:
        from fastapi import HTTPException

        raise HTTPException(status_code=503, detail={
            "code": "AI_ASSISTANT_DISABLED",
            "message": "The AI assistant is not available in this workspace: "
                       + status["reason"] + ". The calculator works without it.",
        })
