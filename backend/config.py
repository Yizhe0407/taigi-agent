"""Central configuration for the Taigi Bus Agent backend.

All env var names and their defaults are declared here so operators
can see the full surface area in one place. Domain tools (tools/)
keep their own lazy os.getenv() calls for test monkeypatching.

Usage
-----
    from config import Settings, make_agent_session

    settings = Settings.from_env()          # raises RuntimeError if required vars missing
    session  = make_agent_session(settings)  # shared factory for HTTP API + CLI
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from openai import OpenAI

from agent.prompt import build_system_prompt
from agent.session import AgentSession, InputEnricher
from agent.telemetry import configure_telemetry
from agent.tools import TOOL_HANDLERS, TOOL_SCHEMAS
from tools.kiosk_bus import prefetch_route_arrival_context


@dataclass(frozen=True)
class Settings:
    """Parsed and validated environment-variable configuration."""

    # ── LLM (required) ────────────────────────────────────────────────────────
    llm_base_url: str
    llm_model: str
    llm_api_key: str                    # default "ollama" for local deployments

    # ── Kiosk identity ────────────────────────────────────────────────────────
    kiosk_stop: str                     # default "雲林科技大學"
    kiosk_direction: str | None         # "去程" | "回程" | None (show both)

    # ── ASR (optional — service is disabled when asr_base_url is None) ────────
    asr_base_url: str | None
    asr_model: str | None
    asr_api_key: str

    # ── TTS (optional — service is disabled when tts_base_url is None) ────────
    tts_base_url: str | None          # e.g. https://tts.example.com
    tts_model: str                    # model name forwarded to /v1/audio/speech
    tts_voice: str                    # voice name forwarded to /v1/audio/speech
    tts_api_key: str                  # empty string = no Authorization header

    # ── HTTP API ───────────────────────────────────────────────────────────────
    cors_origins: list[str]

    @classmethod
    def from_env(cls) -> "Settings":
        """Read and validate settings from environment variables.

        Raises RuntimeError if any *required* variables are absent.
        Optional variables fall back to documented defaults.
        """
        llm_base_url = os.getenv("LLM_BASE_URL", "")
        llm_model = os.getenv("LLM_MODEL", "")
        missing = [
            name
            for name, val in [("LLM_BASE_URL", llm_base_url), ("LLM_MODEL", llm_model)]
            if not val
        ]
        if missing:
            raise RuntimeError(
                f"Required env vars not set: {', '.join(missing)}"
            )

        cors_raw = os.getenv("API_CORS_ORIGINS", "")

        return cls(
            llm_base_url=llm_base_url,
            llm_model=llm_model,
            llm_api_key=os.getenv("LLM_API_KEY", "ollama"),
            kiosk_stop=os.getenv("KIOSK_STOP", "雲林科技大學"),
            kiosk_direction=os.getenv("KIOSK_DIRECTION") or None,
            asr_base_url=os.getenv("ASR_BASE_URL") or None,
            asr_model=os.getenv("ASR_MODEL") or None,
            asr_api_key=os.getenv("ASR_API_KEY", ""),
            tts_base_url=os.getenv("TTS_BASE_URL") or None,
            tts_model=os.getenv("TTS_MODEL", "tts-1"),
            tts_voice=os.getenv("TTS_VOICE", "taigi"),
            tts_api_key=os.getenv("TTS_API_KEY", ""),
            cors_origins=[o.strip() for o in cors_raw.split(",") if o.strip()],
        )


def make_agent_session(
    settings: Settings,
    input_enricher: InputEnricher | None = None,
) -> AgentSession:
    """Create an AgentSession from *settings*.

    Shared factory used by the HTTP API (api/chat.py) and the CLI (agent/loop.py)
    to avoid duplicating the OpenAI client + session wiring.

    *input_enricher* defaults to ``prefetch_route_arrival_context`` so callers
    that don't need to customise it can omit the argument.
    """
    if input_enricher is None:
        input_enricher = prefetch_route_arrival_context

    return AgentSession(
        client=OpenAI(base_url=settings.llm_base_url, api_key=settings.llm_api_key),
        model=settings.llm_model,
        system_prompt=build_system_prompt(),
        tool_schemas=TOOL_SCHEMAS,
        tool_handlers=TOOL_HANDLERS,
        input_enricher=input_enricher,
        telemetry=configure_telemetry(),
    )
