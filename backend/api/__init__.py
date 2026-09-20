"""HTTP API for the Taigi Bus Agent frontend."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from dotenv import load_dotenv

# Must precede all domain imports — modules read env vars at import time.
load_dotenv()

from fastapi import FastAPI  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor  # noqa: E402
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor  # noqa: E402

from async_lifecycle import cancel_and_join_task, create_lifecycle_task, join_task  # noqa: E402
from config import (  # noqa: E402
    _LlmClientLifecycleOwner,
    close_llm_clients,
    parse_cors_origins,
    startup_llm_clients,
)
from pipeline.text_processor import shutdown_text_processor, startup_text_processor  # noqa: E402
from providers.http import aclose_http_client, startup_http_client  # noqa: E402
from services.departures import get_provider  # noqa: E402
from services.kiosk_config import kiosk_stop_name  # noqa: E402
from telemetry import configure_telemetry  # noqa: E402

_log = logging.getLogger(__name__)
_ETA_WARMUP_INTERVAL = 25.0  # slightly under ETA cache TTL (30 s)


async def _eta_warmup_loop() -> None:
    """Keep fetch_eta_at_stop cache warm so user requests never trigger cold upstream calls.

    Re-reads kiosk_stop_name() every iteration (not just once at lifespan
    start) so an admin-triggered stop change is picked up on the next tick
    instead of leaving the new stop cold until a process restart.
    """
    while True:
        # Re-read provider each tick too: set_provider() (boot wiring / region
        # rollout) would otherwise keep warming the old instance forever.
        provider = get_provider()
        stop_name = kiosk_stop_name()
        try:
            await provider.load_route_info(stop_name)  # warms the provider route/ETA caches
            await provider.fetch_eta_at_stop(stop_name)
        except Exception as exc:
            _log.warning("ETA cache warmup failed for %s: %s", stop_name, exc)
        else:
            # Cache is fresh — push the new snapshot to departure SSE clients.
            notify_snapshot_refreshed()
        await asyncio.sleep(_ETA_WARMUP_INTERVAL)


from .admin import router as admin_router  # noqa: E402
from .asr import router as asr_router  # noqa: E402
from .bike import router as bike_router  # noqa: E402
from .chat import close_store, run_lock_purge_loop, startup_store  # noqa: E402
from .chat import router as chat_router  # noqa: E402
from .client_events import router as client_events_router  # noqa: E402
from .departures import (  # noqa: E402
    notify_snapshot_refreshed,
    shutdown_departure_streams,
    startup_departure_streams,
)
from .departures import router as departures_router  # noqa: E402
from .health import router as health_router  # noqa: E402
from .request_limits import RequestBodyLimitMiddleware  # noqa: E402
from .route_plans import router as route_plans_router  # noqa: E402
from .tts import router as tts_router  # noqa: E402
from .voice import router as voice_router  # noqa: E402
from .voice import shutdown as voice_shutdown  # noqa: E402
from .voice import startup as voice_startup  # noqa: E402


def _raise_lifecycle_errors(message: str, errors: list[BaseException]) -> None:
    if len(errors) == 1:
        raise errors[0]
    if errors:
        raise BaseExceptionGroup(message, errors)


@dataclass
class _ApiLifespanResources:
    """Tracks exactly which parts of one API generation still need teardown."""

    app: FastAPI | None = None
    background_tasks: list[asyncio.Task[None]] = field(default_factory=list)
    voice_pending: bool = False
    departures_pending: bool = False
    store_pending: bool = False
    llm_owner: _LlmClientLifecycleOwner | None = None
    llm_pending: bool = False
    text_processor_pending: bool = False
    http_pending: bool = False
    shutdown_task: asyncio.Task[None] | None = None
    closed: bool = False


async def _shutdown_api(resources: _ApiLifespanResources) -> None:
    """Run the app's dependency-ordered physical teardown exactly once."""
    background_tasks = tuple(resources.background_tasks)
    for task in background_tasks:
        if not task.done() and task.cancelling() == 0:
            task.cancel()
    errors: list[BaseException] = []
    for task in background_tasks:
        try:
            await cancel_and_join_task(task)
        except BaseException as error:  # noqa: BLE001 — settle every sibling task
            _log.exception("API background task failed during shutdown: %s", task.get_name())
            errors.append(error)
        finally:
            if task.done() and task in resources.background_tasks:
                resources.background_tasks.remove(task)

    async def run_cleanup(
        pending_attribute: str,
        label: str,
        cleanup: Callable[[], Awaitable[None] | None],
    ) -> None:
        if not getattr(resources, pending_attribute):
            return
        try:
            result = cleanup()
            if result is not None:
                await result
        except BaseException as error:  # noqa: BLE001 — finish all independent closes
            _log.exception("API shutdown cleanup failed: %s", label)
            errors.append(error)
        else:
            setattr(resources, pending_attribute, False)

    async def close_owned_llm() -> None:
        owner = resources.llm_owner
        if owner is None:
            return
        await close_llm_clients(owner)
        app = resources.app
        if app is not None and getattr(app.state, "llm_client_owner", None) is owner:
            del app.state.llm_client_owner
        resources.llm_owner = None

    # Producers stop before consumers and shared dependencies.  Active voice
    # pipelines and departure request streams may still borrow chat/LLM/text or
    # HTTP state while retiring, so those owners remain open until both gates
    # have closed and every admitted unit has exited.  Successful components
    # clear their debt immediately; a later retry touches only failed cleanup.
    await run_cleanup("voice_pending", "voice", voice_shutdown)
    await run_cleanup("departures_pending", "departure streams", shutdown_departure_streams)
    await run_cleanup("store_pending", "chat session store", close_store)
    await run_cleanup("llm_pending", "LLM clients", close_owned_llm)
    await run_cleanup(
        "text_processor_pending",
        "text processor",
        shutdown_text_processor,
    )
    await run_cleanup("http_pending", "shared HTTP client", aclose_http_client)
    _raise_lifecycle_errors("API shutdown failed", errors)


async def _join_api_shutdown(resources: _ApiLifespanResources) -> None:
    """Give physical teardown one cancellation-safe owner task."""
    if resources.closed:
        return
    shutdown_task = resources.shutdown_task
    if shutdown_task is None:
        shutdown_task = create_lifecycle_task(
            _shutdown_api(resources),
            name="api-lifespan-shutdown",
        )
        resources.shutdown_task = shutdown_task
    try:
        await join_task(shutdown_task)
    finally:
        if resources.shutdown_task is shutdown_task and shutdown_task.done():
            if not shutdown_task.cancelled() and shutdown_task.exception() is None:
                resources.closed = True
            resources.shutdown_task = None


def _raise_primary_and_cleanup(
    message: str,
    primary: BaseException,
    cleanup: BaseException,
) -> None:
    if isinstance(primary, asyncio.CancelledError) and isinstance(cleanup, asyncio.CancelledError):
        raise primary
    raise BaseExceptionGroup(message, [primary, cleanup]) from None


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    resources = _ApiLifespanResources(app=app)
    try:
        # A failed prior generation remains the authoritative cleanup owner on
        # app.state. Never overwrite that debt with a successor generation.
        previous_owner = getattr(app.state, "llm_client_owner", None)
        if previous_owner is not None:
            await close_llm_clients(previous_owner)
            if getattr(app.state, "llm_client_owner", None) is previous_owner:
                del app.state.llm_client_owner

        resources.llm_owner = startup_llm_clients()
        resources.llm_pending = True
        app.state.llm_client_owner = resources.llm_owner
        resources.http_pending = True
        startup_http_client()
        resources.departures_pending = True
        await startup_departure_streams()
        resources.text_processor_pending = True
        await startup_text_processor()
        resources.store_pending = True
        await startup_store()
        resources.voice_pending = True
        voice_startup()
        # Every successful acquisition is recorded before the next one starts.
        # The warmup loop performs cold upstream discovery in the background;
        # readiness must not depend on a third-party scan completing.
        resources.background_tasks.append(
            create_lifecycle_task(
                _eta_warmup_loop(),
                name="eta-warmup-loop",
            )
        )
        resources.background_tasks.append(
            create_lifecycle_task(
                run_lock_purge_loop(),
                name="chat-lock-purge-loop",
            )
        )
    except BaseException as startup_error:
        try:
            await _join_api_shutdown(resources)
        except BaseException as cleanup_error:
            _raise_primary_and_cleanup(
                "API startup and rollback both failed",
                startup_error,
                cleanup_error,
            )
        raise

    try:
        yield
    except BaseException as app_error:
        try:
            await _join_api_shutdown(resources)
        except BaseException as cleanup_error:
            _raise_primary_and_cleanup(
                "API lifespan body and shutdown both failed",
                app_error,
                cleanup_error,
            )
        raise
    else:
        # Cancellation belongs to the lifespan waiter, never to physical
        # resource teardown. One shielded owner completes the full sequence
        # before the caller's cancellation is restored.
        await _join_api_shutdown(resources)


app = FastAPI(title="Taigi Bus Agent API", lifespan=_lifespan)
app.add_middleware(RequestBodyLimitMiddleware)

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
app.include_router(health_router)
app.include_router(route_plans_router)
app.include_router(bike_router)
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
