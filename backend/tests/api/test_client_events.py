"""Tests for POST /api/client-events frontend error reporting."""

import pytest
from fastapi.testclient import TestClient

import api
import api.client_events


@pytest.fixture()
def client() -> TestClient:
    return TestClient(api.app, raise_server_exceptions=False)


def test_client_event_accepted_and_logged(client: TestClient, monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(
        api.client_events,
        "log_diagnostic",
        lambda scope, message: captured.update(scope=scope, message=message),
    )

    response = client.post(
        "/api/client-events",
        json={"type": "webrtc_ice_failed", "message": "ICE connection failed", "ts": 123.0},
    )

    assert response.status_code == 204
    assert captured["scope"] == "client"
    assert "webrtc_ice_failed" in captured["message"]
    assert "ICE connection failed" in captured["message"]
    assert "ts=123.0" in captured["message"]


def test_client_event_truncates_oversized_fields(client: TestClient, monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(
        api.client_events,
        "log_diagnostic",
        lambda scope, message: captured.update(scope=scope, message=message),
    )

    response = client.post(
        "/api/client-events",
        json={
            "type": "uncaught_exception",
            "message": "x" * 1000,
            "detail": "y" * 3000,
        },
    )

    assert response.status_code == 204
    message = captured["message"]
    assert "x" * 501 not in message
    assert "…[truncated]" in message
    assert "y" * 2001 not in message


def test_client_event_rejects_missing_required_field(client: TestClient):
    response = client.post("/api/client-events", json={"message": "no type field"})
    assert response.status_code == 422


def test_client_event_rejects_wrong_type(client: TestClient):
    response = client.post(
        "/api/client-events",
        json={"type": "uncaught_exception", "message": 12345},
    )
    assert response.status_code == 422
