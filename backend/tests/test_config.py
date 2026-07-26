import pytest

from config import Settings, _make_llm_client, parse_cors_origins


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


def test_settings_requires_llm_env(monkeypatch):
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)

    with pytest.raises(RuntimeError, match="LLM_BASE_URL, LLM_MODEL"):
        Settings.from_env()


def test_settings_local_llm_env_satisfies_requirement(monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", "http://llm.local/v1")
    monkeypatch.setenv("LLM_MODEL", "qwen3-4b")
    monkeypatch.delenv("LLM_API_KEY", raising=False)

    s = Settings.from_env()
    assert s.llm_base_url == "http://llm.local/v1"
    assert s.llm_model == "qwen3-4b"
    assert s.llm_api_key == "ollama"
    assert s.llm_extra_body["chat_template_kwargs"] == {"enable_thinking": False}
    # Anti-degeneration sampling merged into every backend's extra_body.
    assert s.llm_extra_body["max_tokens"] == 200
    assert s.llm_extra_body["stop"] == ["\n\n"]
    # Guard: repetition penalties must never come back — llama.cpp's penalty
    # window includes the prompt tail, which corrupts tool-call JSON and
    # punishes the verbatim tool-text copying the renderers depend on.
    assert "frequency_penalty" not in s.llm_extra_body
    assert "repeat_penalty" not in s.llm_extra_body


def test_llm_client_has_one_retry_owner_and_bounded_timeouts(monkeypatch):
    monkeypatch.setenv("LLM_READ_TIMEOUT_SECONDS", "42")
    _make_llm_client.cache_clear()

    client = _make_llm_client("http://llm.local/v1", "test")

    assert client.max_retries == 0
    assert client.timeout.connect == 5
    assert client.timeout.read == 42
    assert client.timeout.write == 15
    assert client.timeout.pool == 5
    _make_llm_client.cache_clear()
