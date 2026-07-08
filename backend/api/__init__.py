"""HTTP API for the Taigi Bus Agent frontend."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

from dotenv import load_dotenv

# Must precede all domain imports — modules read env vars at import time.
load_dotenv()

from fastapi import FastAPI  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor  # noqa: E402
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor  # noqa: E402

from config import parse_cors_origins  # noqa: E402
from providers.http import aclose_http_client  # noqa: E402
from services.departures import get_provider  # noqa: E402
from services.kiosk_config import kiosk_stop_name  # noqa: E402
from telemetry import configure_telemetry  # noqa: E402

_log = logging.getLogger(__name__)
_ETA_WARMUP_INTERVAL = 25.0  # slightly under ETA cache TTL (30 s)


async def _eta_warmup_loop(stop_name: str) -> None:
    """Keep fetch_eta_at_stop cache warm so user requests never trigger TDX calls."""
    provider = get_provider()
    while True:
        try:
            await provider.load_route_info(stop_name)  # warms _kiosk_uids (TTL 600 s)
            await provider.fetch_eta_at_stop(stop_name)
        except Exception as exc:
            _log.warning("ETA cache warmup failed for %s: %s", stop_name, exc)
        await asyncio.sleep(_ETA_WARMUP_INTERVAL)


from .admin import router as admin_router  # noqa: E402
from .asr import router as asr_router  # noqa: E402
from .chat import router as chat_router  # noqa: E402
from .client_events import router as client_events_router  # noqa: E402
from .departures import router as departures_router  # noqa: E402
from .moovo import router as moovo_router  # noqa: E402
from .route_plans import router as route_plans_router  # noqa: E402
from .tts import router as tts_router  # noqa: E402
from .voice import router as voice_router  # noqa: E402


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    stop_name = kiosk_stop_name()
    provider = get_provider()
    # Pre-fetch ebus route map + discover routes at this stop so warmup loop is cold-cache-free.
    if hasattr(provider, "warmup"):
        try:
            await provider.warmup(stop_name)
        except Exception as exc:
            _log.warning("provider warmup failed: %s", exc)
    elif hasattr(provider, "warmup_route_map"):
        try:
            await provider.warmup_route_map()
        except Exception as exc:
            _log.warning("ebus route map warmup failed: %s", exc)
    warmup_task = asyncio.create_task(_eta_warmup_loop(stop_name))
    try:
        yield
    finally:
        warmup_task.cancel()
        with suppress(asyncio.CancelledError):
            await warmup_task
        provider = get_provider()
        if hasattr(provider, "aclose"):
            await provider.aclose()
        await aclose_http_client()


app = FastAPI(title="Taigi Bus Agent API", lifespan=_lifespan)

cors_origins = parse_cors_origins()
if cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["*"],
        expose_headers=["*"],
    )

app.include_router(admin_router)
app.include_router(chat_router)
app.include_router(client_events_router)
app.include_router(departures_router)
app.include_router(route_plans_router)
app.include_router(moovo_router)
app.include_router(asr_router)
app.include_router(tts_router)
app.include_router(voice_router)

# ── Observability ─────────────────────────────────────────────────────────────
# configure_telemetry() is idempotent; safe to call here and in make_agent_session().
# FastAPIInstrumentor: auto-spans every route with http.server.request.duration.
# HTTPXClientInstrumentor: auto-traces all httpx.AsyncClient calls (ASR / TTS
#   upstreams) with server.address, http.request.method, http.response.status_code.
configure_telemetry()
FastAPIInstrumentor.instrument_app(app)
HTTPXClientInstrumentor().instrument()
