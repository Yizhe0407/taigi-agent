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


def test_settings_requires_groq_or_llm_env(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)

    with pytest.raises(RuntimeError, match="GROQ_API_KEY or LLM_BASE_URL"):
        Settings.from_env()


def test_settings_groq_api_key_satisfies_requirement(monkeypatch):
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.delenv("GROQ_MODEL", raising=False)
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test")

    s = Settings.from_env()
    assert s.llm_base_url == "https://api.groq.com/openai/v1"
    assert s.llm_model == "qwen/qwen3-32b"
    assert s.llm_api_key == "gsk_test"
    assert s.llm_extra_body["reasoning_format"] == "hidden"
    # Anti-degeneration sampling merged into every backend's extra_body.
    assert s.llm_extra_body["max_tokens"] == 200
    assert s.llm_extra_body["stop"] == ["\n\n"]
    # Guard: repetition penalties must never come back — llama.cpp's penalty
    # window includes the prompt tail, which corrupts tool-call JSON and
    # punishes the verbatim tool-text copying the renderers depend on.
    assert "frequency_penalty" not in s.llm_extra_body
    assert "repeat_penalty" not in s.llm_extra_body
