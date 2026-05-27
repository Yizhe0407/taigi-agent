"""HTTP API for the Taigi Bus Agent frontend."""

from __future__ import annotations

from dotenv import load_dotenv

# Must precede all domain imports — modules read env vars at import time.
load_dotenv()

from fastapi import FastAPI  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor  # noqa: E402
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor  # noqa: E402

from agent.telemetry import configure_telemetry  # noqa: E402
from config import Settings  # noqa: E402

from .asr import router as asr_router  # noqa: E402
from .chat import router as chat_router  # noqa: E402
from .departures import router as departures_router  # noqa: E402
from .moovo import router as moovo_router  # noqa: E402
from .route_plans import router as route_plans_router  # noqa: E402
from .tts import router as tts_router  # noqa: E402

app = FastAPI(title="Taigi Bus Agent API")

# Settings has its own CORS parsing; reuse it so the env contract has a
# single source. Settings.from_env() may raise if LLM_* are missing — when
# that happens we still want CORS off rather than blocking the API import.
try:
    cors_origins = Settings.from_env().cors_origins
except RuntimeError:
    cors_origins = []

if cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["*"],
        expose_headers=["*"],
    )

app.include_router(chat_router)
app.include_router(departures_router)
app.include_router(route_plans_router)
app.include_router(moovo_router)
app.include_router(asr_router)
app.include_router(tts_router)

# ── Observability ─────────────────────────────────────────────────────────────
# configure_telemetry() is idempotent; safe to call here and in make_agent_session().
# FastAPIInstrumentor: auto-spans every route with http.server.request.duration.
# HTTPXClientInstrumentor: auto-traces all httpx.AsyncClient calls (ASR / TTS
#   upstreams) with server.address, http.request.method, http.response.status_code.
configure_telemetry()
FastAPIInstrumentor.instrument_app(app)
HTTPXClientInstrumentor().instrument()
