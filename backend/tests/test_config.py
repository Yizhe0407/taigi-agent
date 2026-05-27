import pytest

from config import Settings, parse_cors_origins


def test_parse_cors_origins_does_not_require_llm_env(monkeypatch):
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.setenv(
        "API_CORS_ORIGINS",
        "http://localhost:5173, https://kiosk.example.tw ,",
    )

    assert parse_cors_origins() == [
        "http://localhost:5173",
        "https://kiosk.example.tw",
    ]


def test_settings_still_requires_llm_env(monkeypatch):
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)

    with pytest.raises(RuntimeError, match="LLM_BASE_URL, LLM_MODEL"):
        Settings.from_env()
