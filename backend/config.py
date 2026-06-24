"""Central configuration for the Taigi Bus Agent backend.

All env var names and their defaults are declared here so operators
can see the full surface area in one place. Domain tools (tools/)
keep their own lazy os.getenv() calls for test monkeypatching.

Usage
-----
    from config import Settings, make_agent_session

    settings = Settings.from_env()          # validates required env vars
    session  = make_agent_session(settings)  # shared factory for HTTP API + CLI
"""

from __future__ import annotations

import functools
import os
from dataclasses import dataclass

from openai import AsyncOpenAI

from agent.prompt import build_system_prompt
from agent.session import AgentSession, InputEnricher
from agent.tools import TOOL_HANDLERS, TOOL_SCHEMAS
from telemetry import configure_telemetry

_GROQ_BASE_URL = "https://api.groq.com/openai/v1"
_GROQ_DEFAULT_MODEL = "qwen/qwen3-32b"
# vLLM (Qwen3) requires this to suppress chain-of-thought tokens.
_VLLM_EXTRA_BODY = {"chat_template_kwargs": {"enable_thinking": False}}


def parse_cors_origins() -> list[str]:
    """Parse API_CORS_ORIGINS without requiring LLM settings."""
    cors_raw = os.getenv("API_CORS_ORIGINS", "")
    return [origin.strip() for origin in cors_raw.split(",") if origin.strip()]


@dataclass(frozen=True)
class Settings:
    """Parsed and validated environment-variable configuration."""

    # ── LLM ───────────────────────────────────────────────────────────────────
    # Set GROQ_API_KEY to route through Groq (preferred).
    # Otherwise set LLM_BASE_URL + LLM_MODEL for a local/vLLM endpoint.
    llm_base_url: str
    llm_model: str
    llm_api_key: str
    llm_extra_body: dict

    # ── ASR (optional — service is disabled when asr_base_url is None) ────────
    asr_base_url: str | None
    asr_model: str | None
    asr_api_key: str

    # ── TTS (optional — service is disabled when tts_base_url is None) ────────
    tts_base_url: str | None  # e.g. https://tts.example.com
    tts_model: str  # model name forwarded to /v1/audio/speech
    tts_voice: str  # voice name forwarded to /v1/audio/speech
    tts_api_key: str  # empty string = no Authorization header

    # ── HTTP API ───────────────────────────────────────────────────────────────
    cors_origins: list[str]

    @classmethod
    def from_env(cls) -> Settings:
        """Read and validate settings from environment variables.

        Raises RuntimeError if any *required* variables are absent.
        Optional variables fall back to documented defaults.
        """
        groq_api_key = os.getenv("GROQ_API_KEY", "")
        if groq_api_key:
            llm_base_url = _GROQ_BASE_URL
            llm_model = os.getenv("GROQ_MODEL", _GROQ_DEFAULT_MODEL)
            llm_api_key = groq_api_key
            llm_extra_body: dict = {}
        else:
            llm_base_url = os.getenv("LLM_BASE_URL", "")
            llm_model = os.getenv("LLM_MODEL", "")
            missing = [name for name, val in [("LLM_BASE_URL", llm_base_url), ("LLM_MODEL", llm_model)] if not val]
            if missing:
                raise RuntimeError(f"Required env vars not set: GROQ_API_KEY or {', '.join(missing)}")
            llm_api_key = os.getenv("LLM_API_KEY", "ollama")
            llm_extra_body = _VLLM_EXTRA_BODY

        return cls(
            llm_base_url=llm_base_url,
            llm_model=llm_model,
            llm_api_key=llm_api_key,
            llm_extra_body=llm_extra_body,
            asr_base_url=os.getenv("ASR_BASE_URL") or None,
            asr_model=os.getenv("ASR_MODEL") or None,
            asr_api_key=os.getenv("ASR_API_KEY", ""),
            tts_base_url=os.getenv("TTS_BASE_URL") or None,
            tts_model=os.getenv("TTS_MODEL", "tts-1"),
            tts_voice=os.getenv("TTS_VOICE", "taigi"),
            tts_api_key=os.getenv("TTS_API_KEY", ""),
            cors_origins=parse_cors_origins(),
        )


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide singleton. Re-call Settings.from_env() directly in tests."""
    return Settings.from_env()


@functools.lru_cache(maxsize=4)
def _make_llm_client(base_url: str, api_key: str) -> AsyncOpenAI:
    return AsyncOpenAI(base_url=base_url, api_key=api_key)


def make_agent_session(
    settings: Settings,
    input_enricher: InputEnricher | None = None,
) -> AgentSession:
    """Create an AgentSession from *settings*.

    Shared factory used by the HTTP API (api/chat.py) and the CLI (agent/loop.py)
    to avoid duplicating the OpenAI client + session wiring.
    """
    return AgentSession(
        client=_make_llm_client(settings.llm_base_url, settings.llm_api_key),
        model=settings.llm_model,
        system_prompt=build_system_prompt(),
        tool_schemas=TOOL_SCHEMAS,
        tool_handlers=TOOL_HANDLERS,
        input_enricher=input_enricher,
        extra_body=settings.llm_extra_body,
        telemetry=configure_telemetry(),
    )
