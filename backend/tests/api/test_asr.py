"""Tests for POST /api/asr transcription proxy."""

import io

import httpx
import pytest
from fastapi.testclient import TestClient

import api
import api.asr


@pytest.fixture()
def client() -> TestClient:
    return TestClient(api.app, raise_server_exceptions=False)


def _webm_stub() -> bytes:
    """Minimal non-empty bytes that stand in for a real webm audio file."""
    return b"\x1a\x45\xdf\xa3" + b"\x00" * 16


def _ok_response(text: str = "201 幾分到") -> httpx.Response:
    return httpx.Response(200, json={"text": text})


# ---------------------------------------------------------------------------
# Config guard
# ---------------------------------------------------------------------------


def test_asr_returns_503_when_not_configured(client: TestClient, monkeypatch):
    monkeypatch.delenv("ASR_BASE_URL", raising=False)
    monkeypatch.delenv("ASR_MODEL", raising=False)

    response = client.post(
        "/api/asr",
        files={"file": ("audio.webm", _webm_stub(), "audio/webm")},
    )

    assert response.status_code == 503
    assert "ASR_BASE_URL" in response.json()["detail"]


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


def test_asr_returns_400_for_empty_file(client: TestClient, monkeypatch):
    monkeypatch.setenv("ASR_BASE_URL", "http://asr.local")
    monkeypatch.setenv("ASR_MODEL", "qwen3-asr")

    response = client.post(
        "/api/asr",
        files={"file": ("audio.webm", b"", "audio/webm")},
    )

    assert response.status_code == 400
    assert "空" in response.json()["detail"]


def test_asr_returns_413_for_oversized_file(client: TestClient, monkeypatch):
    monkeypatch.setenv("ASR_BASE_URL", "http://asr.local")
    monkeypatch.setenv("ASR_MODEL", "qwen3-asr")

    oversized = io.BytesIO(b"\x00" * (25 * 1024 * 1024 + 1))
    response = client.post(
        "/api/asr",
        files={"file": ("audio.webm", oversized, "audio/webm")},
    )

    assert response.status_code == 413


# ---------------------------------------------------------------------------
# Fix 1: tests patch api._asr_post_audio, not httpx internals
# ---------------------------------------------------------------------------


def test_asr_returns_text_from_upstream(client: TestClient, monkeypatch):
    monkeypatch.setenv("ASR_BASE_URL", "http://asr.local")
    monkeypatch.setenv("ASR_MODEL", "qwen3-asr")

    async def fake_post(url, headers, filename, audio_bytes, content_type, model):
        return _ok_response("201 幾分到")

    monkeypatch.setattr(api.asr, "_asr_post_audio", fake_post)

    response = client.post(
        "/api/asr",
        files={"file": ("audio.webm", _webm_stub(), "audio/webm")},
    )

    assert response.status_code == 200
    assert response.json() == {"text": "201 幾分到"}


def test_asr_returns_422_for_empty_transcription(client: TestClient, monkeypatch):
    monkeypatch.setenv("ASR_BASE_URL", "http://asr.local")
    monkeypatch.setenv("ASR_MODEL", "qwen3-asr")

    async def fake_post(url, headers, filename, audio_bytes, content_type, model):
        return httpx.Response(200, json={"text": "   "})

    monkeypatch.setattr(api.asr, "_asr_post_audio", fake_post)

    response = client.post(
        "/api/asr",
        files={"file": ("audio.webm", _webm_stub(), "audio/webm")},
    )

    assert response.status_code == 422
    assert "未聽清楚" in response.json()["detail"]


def test_asr_returns_502_for_upstream_error(client: TestClient, monkeypatch):
    monkeypatch.setenv("ASR_BASE_URL", "http://asr.local")
    monkeypatch.setenv("ASR_MODEL", "qwen3-asr")

    async def fake_post(url, headers, filename, audio_bytes, content_type, model):
        return httpx.Response(500, text="Internal Server Error")

    monkeypatch.setattr(api.asr, "_asr_post_audio", fake_post)

    response = client.post(
        "/api/asr",
        files={"file": ("audio.webm", _webm_stub(), "audio/webm")},
    )

    assert response.status_code == 502


def test_asr_returns_504_on_timeout(client: TestClient, monkeypatch):
    monkeypatch.setenv("ASR_BASE_URL", "http://asr.local")
    monkeypatch.setenv("ASR_MODEL", "qwen3-asr")

    async def fake_post(url, headers, filename, audio_bytes, content_type, model):
        raise httpx.TimeoutException("timed out")

    monkeypatch.setattr(api.asr, "_asr_post_audio", fake_post)

    response = client.post(
        "/api/asr",
        files={"file": ("audio.webm", _webm_stub(), "audio/webm")},
    )

    assert response.status_code == 504
    assert "逾時" in response.json()["detail"]


def test_asr_returns_503_on_connection_error(client: TestClient, monkeypatch):
    monkeypatch.setenv("ASR_BASE_URL", "http://asr.local")
    monkeypatch.setenv("ASR_MODEL", "qwen3-asr")

    async def fake_post(url, headers, filename, audio_bytes, content_type, model):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(api.asr, "_asr_post_audio", fake_post)

    response = client.post(
        "/api/asr",
        files={"file": ("audio.webm", _webm_stub(), "audio/webm")},
    )

    assert response.status_code == 503
    assert "無法連線" in response.json()["detail"]


# ---------------------------------------------------------------------------
# Fix 3: ASR_API_KEY forwarding behaviour
# ---------------------------------------------------------------------------


def test_asr_sends_authorization_header_when_key_is_set(client: TestClient, monkeypatch):
    monkeypatch.setenv("ASR_BASE_URL", "http://asr.local")
    monkeypatch.setenv("ASR_MODEL", "qwen3-asr")
    monkeypatch.setenv("ASR_API_KEY", "secret-token")

    captured: dict = {}

    async def fake_post(url, headers, filename, audio_bytes, content_type, model):
        captured["headers"] = headers
        return _ok_response()

    monkeypatch.setattr(api.asr, "_asr_post_audio", fake_post)
    client.post(
        "/api/asr",
        files={"file": ("audio.webm", _webm_stub(), "audio/webm")},
    )

    assert captured["headers"].get("Authorization") == "Bearer secret-token"


def test_asr_omits_authorization_header_when_key_is_empty(client: TestClient, monkeypatch):
    monkeypatch.setenv("ASR_BASE_URL", "http://asr.local")
    monkeypatch.setenv("ASR_MODEL", "qwen3-asr")
    monkeypatch.delenv("ASR_API_KEY", raising=False)

    captured: dict = {}

    async def fake_post(url, headers, filename, audio_bytes, content_type, model):
        captured["headers"] = headers
        return _ok_response()

    monkeypatch.setattr(api.asr, "_asr_post_audio", fake_post)
    client.post(
        "/api/asr",
        files={"file": ("audio.webm", _webm_stub(), "audio/webm")},
    )

    assert "Authorization" not in captured["headers"]
