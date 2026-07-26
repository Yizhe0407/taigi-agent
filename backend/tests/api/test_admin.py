"""Security and canonical-data tests for the admin kiosk API."""

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import api.admin
from providers.otp import Coordinate
from services.kiosk_config import KioskConfig
from services.stop_catalog import StopCatalog, StopRecord


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(api.admin.router)
    return TestClient(app)


def _catalog() -> StopCatalog:
    return StopCatalog(
        stops=(
            StopRecord("A", "雲林科技大學", Coordinate(23.6950, 120.5340)),
            StopRecord("B", "雲林科技大學", Coordinate(23.6952, 120.5342)),
        )
    )


def test_admin_write_fails_closed_when_token_is_unset(monkeypatch):
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)

    response = _client().put(
        "/api/admin/kiosk",
        json={"stop_name": "雲林科技大學", "direction": "回程"},
    )

    assert response.status_code == 503


def test_admin_write_rejects_invalid_token(monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "correct-token")

    response = _client().put(
        "/api/admin/kiosk",
        headers={"X-Admin-Token": "wrong-token"},
        json={"stop_name": "雲林科技大學", "direction": "回程"},
    )

    assert response.status_code == 401


def test_admin_token_comparison_handles_non_ascii_input(monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "correct-token")

    with pytest.raises(HTTPException) as error:
        api.admin._require_admin_token("錯誤")

    assert error.value.status_code == 401


def test_admin_write_uses_catalog_coordinates(monkeypatch):
    saved: list[KioskConfig] = []
    monkeypatch.setenv("ADMIN_TOKEN", "correct-token")
    monkeypatch.setattr(api.admin, "load_stop_catalog", _catalog)
    monkeypatch.setattr(api.admin, "set_kiosk_config", saved.append)

    response = _client().put(
        "/api/admin/kiosk",
        headers={"X-Admin-Token": "correct-token"},
        json={
            "stop_name": "雲林科技大學",
            "direction": "回程",
            "lat": 0,
            "lng": 0,
        },
    )

    assert response.status_code == 200
    assert saved == [
        KioskConfig(
            stop_name="雲林科技大學",
            direction="回程",
            lat=23.6951,
            lon=120.5341,
        )
    ]
    assert response.json()["lat"] == 23.6951
    assert response.json()["lng"] == 120.5341


def test_admin_write_rejects_unknown_stop(monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "correct-token")
    monkeypatch.setattr(api.admin, "load_stop_catalog", _catalog)

    response = _client().put(
        "/api/admin/kiosk",
        headers={"X-Admin-Token": "correct-token"},
        json={"stop_name": "不存在的站牌", "direction": None},
    )

    assert response.status_code == 422
