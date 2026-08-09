"""Tests for the production liveness endpoint."""

from fastapi.testclient import TestClient

import api


def test_health_reports_process_liveness_without_caching() -> None:
    client = TestClient(api.app, raise_server_exceptions=False)

    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert response.headers["cache-control"] == "no-store"
