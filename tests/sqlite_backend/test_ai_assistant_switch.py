"""The AI assistant must switch off cleanly where there is no model endpoint."""
import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))


@pytest.fixture
def features(monkeypatch):
    module = importlib.import_module("app.features")
    monkeypatch.setattr(module, "_cached", None)
    return module


def test_off_by_switch(features, monkeypatch):
    monkeypatch.setenv("LAKEMETER_AI_ASSISTANT", "off")
    assert features.ai_assistant_status()["enabled"] is False


def test_on_by_switch_skips_probe(features, monkeypatch):
    monkeypatch.setenv("LAKEMETER_AI_ASSISTANT", "on")
    assert features.ai_assistant_status()["enabled"] is True


def test_auto_without_workspace_is_off(features, monkeypatch):
    monkeypatch.delenv("LAKEMETER_AI_ASSISTANT", raising=False)
    monkeypatch.delenv("DATABRICKS_HOST", raising=False)
    status = features.ai_assistant_status()
    assert status["enabled"] is False and "workspace" in status["reason"]


def test_auto_with_missing_endpoint_is_off(features, monkeypatch):
    """Air-gapped: the SDK call fails (no such endpoint / no FMAPI)."""
    monkeypatch.delenv("LAKEMETER_AI_ASSISTANT", raising=False)
    monkeypatch.setenv("DATABRICKS_HOST", "https://workspace.example.invalid")

    class Endpoints:
        def get(self, name):
            raise RuntimeError(f"RESOURCE_DOES_NOT_EXIST: {name}")

    class Client:
        serving_endpoints = Endpoints()

    import databricks.sdk
    monkeypatch.setattr(databricks.sdk, "WorkspaceClient", lambda: Client())
    status = features.ai_assistant_status()
    assert status["enabled"] is False and "not available" in status["reason"]


def test_chat_route_answers_503_when_off(features, monkeypatch, tmp_path):
    monkeypatch.setenv("LAKEMETER_AI_ASSISTANT", "off")
    monkeypatch.setenv("DB_BACKEND", "sqlite")
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "chat.db"))
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as client:
        response = client.post("/api/v1/chat/", json={"message": "hello"},
                               headers={"X-Forwarded-Email": "chat@example.com"})
        assert response.status_code == 503
        assert response.json()["detail"]["code"] == "AI_ASSISTANT_DISABLED"
        assert client.get("/api/v1/system/features").json()["ai_assistant"]["enabled"] is False


def test_stream_route_also_guarded_and_state_routes_are_not(features, monkeypatch, tmp_path):
    monkeypatch.setenv("LAKEMETER_AI_ASSISTANT", "off")
    monkeypatch.setenv("DB_BACKEND", "sqlite")
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "chat2.db"))
    from fastapi.testclient import TestClient
    from app.main import app

    headers = {"X-Forwarded-Email": "chat@example.com"}
    with TestClient(app) as client:
        assert client.post("/api/v1/chat/stream", json={"message": "hi"}, headers=headers).status_code == 503
        # No model involved: an unknown conversation is a plain 404, not a 503.
        assert client.post("/api/v1/chat/nope/apply", headers=headers).status_code == 404
