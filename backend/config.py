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

import asyncio
import concurrent.futures
import functools
import logging
import os
import threading
import weakref
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

import httpx
from openai import AsyncOpenAI

from agent.prompt import build_system_prompt
from agent.session import AgentSession, InputEnricher
from agent.tools import TOOL_HANDLERS, TOOL_SCHEMAS
from async_lifecycle import AsyncResourceOwner, create_lifecycle_task, join_task
from providers.cloudflare_access import access_headers
from telemetry import configure_telemetry

_log = logging.getLogger(__name__)

# Anti-degeneration sampling. On confirmation turns ("對") Qwen3.5-4B loops the
# same 1-2 sentences separated by blank lines until timeout; stopping at the
# first blank line ends the turn after the (complete, ≤2-sentence) first block,
# and max_tokens is the backstop when no blank line appears. Do NOT add
# repetition/frequency penalties here: llama.cpp applies them over a context
# window that includes the prompt tail, which corrupts tool-call JSON
# (penalized closing quote → 500s) and punishes the verbatim tool-text copying
# this agent's renderers rely on. [eval v5 hole #2]
_SAMPLING = {"max_tokens": 200, "stop": ["\n\n"]}
# Local Qwen3 endpoints (vLLM / llama.cpp) read this to suppress thinking tokens.
# Confirmed accepted by llama-server's OpenAI endpoint (returns 200, thinking off).
_LOCAL_EXTRA_BODY = {"chat_template_kwargs": {"enable_thinking": False}, **_SAMPLING}


def parse_cors_origins() -> list[str]:
    """Parse API_CORS_ORIGINS without requiring LLM settings."""
    cors_raw = os.getenv("API_CORS_ORIGINS", "")
    return [origin.strip() for origin in cors_raw.split(",") if origin.strip()]


@dataclass(frozen=True)
class Settings:
    """Parsed and validated environment-variable configuration."""

    # ── LLM ───────────────────────────────────────────────────────────────────
    # Set LLM_BASE_URL + LLM_MODEL for a local OpenAI-compatible endpoint (vLLM / llama.cpp).
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

    # ── Cloudflare Access (optional service-token headers) ───────────────────
    cf_access_client_id: str
    cf_access_client_secret: str

    # ── HTTP API ───────────────────────────────────────────────────────────────
    cors_origins: list[str]

    @classmethod
    def from_env(cls) -> Settings:
        """Read and validate settings from environment variables.

        Raises RuntimeError if any *required* variables are absent.
        Optional variables fall back to documented defaults.
        """
        llm_base_url = os.getenv("LLM_BASE_URL", "")
        llm_model = os.getenv("LLM_MODEL", "")
        missing = [name for name, val in [("LLM_BASE_URL", llm_base_url), ("LLM_MODEL", llm_model)] if not val]
        if missing:
            raise RuntimeError(f"Required env vars not set: {', '.join(missing)}")
        llm_api_key = os.getenv("LLM_API_KEY", "ollama")
        llm_extra_body: dict = _LOCAL_EXTRA_BODY

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
            cf_access_client_id=os.getenv("CF_ACCESS_CLIENT_ID", ""),
            cf_access_client_secret=os.getenv("CF_ACCESS_CLIENT_SECRET", ""),
            cors_origins=parse_cors_origins(),
        )


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide singleton. Re-call Settings.from_env() directly in tests."""
    return Settings.from_env()


def _build_llm_client(
    base_url: str,
    api_key: str,
    cf_access_client_id: str = "",
    cf_access_client_secret: str = "",
) -> AsyncOpenAI:
    timeout = httpx.Timeout(
        connect=float(os.getenv("LLM_CONNECT_TIMEOUT_SECONDS", "5")),
        read=float(os.getenv("LLM_READ_TIMEOUT_SECONDS", "60")),
        write=float(os.getenv("LLM_WRITE_TIMEOUT_SECONDS", "15")),
        pool=float(os.getenv("LLM_POOL_TIMEOUT_SECONDS", "5")),
    )
    # Retry ownership stays in agent.llm_client so one observable attempt maps
    # to exactly one HTTP attempt. SDK retries would otherwise multiply it.
    return AsyncOpenAI(
        base_url=base_url,
        api_key=api_key,
        timeout=timeout,
        max_retries=0,
        default_headers=access_headers(cf_access_client_id, cf_access_client_secret),
    )


_ClientKey = tuple[str, str, str, str]


@dataclass(eq=False)
class _LlmClientEntry:
    """One HTTP pool and the authoritative owner of every request using it."""

    client: AsyncOpenAI
    http_owner: AsyncResourceOwner[Any]


@dataclass
class _LoopClientCacheState:
    """All live and retiring client generations owned by one lifecycle owner."""

    bucket: OrderedDict[_ClientKey, _LlmClientEntry] = field(default_factory=OrderedDict)
    retired: list[_LlmClientEntry] = field(default_factory=list)
    failed: list[_LlmClientEntry] = field(default_factory=list)
    closer: asyncio.Task[None] | None = None
    background_errors: list[BaseException] = field(default_factory=list)
    closing: bool = False
    shutdown_task: asyncio.Task[None] | None = None


@dataclass(frozen=True)
class LlmClientCleanupDebt:
    """Observable resources that could not be closed after their loop died."""

    live: int
    retired: int
    failed: int
    background_errors: int
    closer_pending: bool
    shutdown_pending: bool


class LlmClientOwnerLoopClosedError(RuntimeError):
    """The owning loop was permanently closed before physical cleanup."""

    def __init__(self, debt: LlmClientCleanupDebt) -> None:
        self.debt = debt
        super().__init__(
            "LLM client owner loop is closed with cleanup debt "
            f"(live={debt.live}, retired={debt.retired}, failed={debt.failed}, "
            f"background_errors={debt.background_errors}, "
            f"closer_pending={debt.closer_pending}, shutdown_pending={debt.shutdown_pending})"
        )


class _LlmClientLifecycleOwner:
    """Strong lifecycle authority for one loop-local cache generation."""

    __slots__ = ("_cache", "_loop", "_state", "_terminal_error", "_closed", "__weakref__")

    def __init__(
        self,
        cache: _LlmClientCache,
        loop: asyncio.AbstractEventLoop,
        state: _LoopClientCacheState,
    ) -> None:
        self._cache = cache
        self._loop: asyncio.AbstractEventLoop | None = loop
        self._state: _LoopClientCacheState | None = state
        self._terminal_error: LlmClientOwnerLoopClosedError | None = None
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def terminal_error(self) -> LlmClientOwnerLoopClosedError | None:
        return self._terminal_error

    @property
    def cleanup_debt(self) -> LlmClientCleanupDebt | None:
        state = self._state
        return None if state is None else self._cache._debt_snapshot(state)

    async def aclose(self) -> None:
        """Close this exact generation, dispatching to its owner loop if needed."""
        await self._cache._aclose_owner(self)


_LoopRef = weakref.ReferenceType[asyncio.AbstractEventLoop]
_OwnerRef = weakref.ReferenceType[_LlmClientLifecycleOwner]


class _LlmClientCache:
    """Loop-local LRU whose process singleton is only a weak lookup index.

    The explicit ``_LlmClientLifecycleOwner`` is the sole strong authority for
    its loop, state, clients, tasks, and retryable cleanup debt. The process
    singleton stores weak references in both directions, so it cannot pin an
    abandoned event loop. All physical AsyncOpenAI/httpx cleanup is dispatched
    to the loop that created the pool.
    """

    def __init__(self, maxsize: int) -> None:
        if maxsize < 1:
            raise ValueError("LLM client cache maxsize must be positive")
        self.maxsize = maxsize
        self._index_lock = threading.Lock()
        self._owners: dict[int, tuple[_LoopRef, _OwnerRef]] = {}

    def startup_current_loop(self) -> _LlmClientLifecycleOwner:
        """Create and return the strong lifecycle owner for the running loop."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError as error:
            raise RuntimeError("LLM clients must be started inside their owning event loop") from error

        existing = self._lookup_owner(loop)
        if existing is not None:
            if existing._state is not None and not existing._state.closing:
                return existing
            raise RuntimeError("LLM client cache is shutting down on this event loop")

        owner = _LlmClientLifecycleOwner(self, loop, _LoopClientCacheState())
        token = id(loop)

        def discard_stale(_reference: object) -> None:
            with self._index_lock:
                current = self._owners.get(token)
                if current is not None and (current[0]() is None or current[1]() is None):
                    self._owners.pop(token, None)

        loop_ref = weakref.ref(loop, discard_stale)
        owner_ref = weakref.ref(owner, discard_stale)
        with self._index_lock:
            current = self._owners.get(token)
            if current is not None and current[0]() is loop:
                current_owner = current[1]()
                if current_owner is not None:
                    return current_owner
            self._owners[token] = (loop_ref, owner_ref)
        return owner

    def get_session_resources(
        self,
        base_url: str,
        api_key: str,
        cf_access_client_id: str = "",
        cf_access_client_secret: str = "",
    ) -> tuple[AsyncOpenAI, AsyncResourceOwner[Any]]:
        """Return one inseparable client/request-owner generation."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError as error:
            raise RuntimeError("LLM clients must be created inside their owning event loop") from error

        owner = self._lookup_owner(loop)
        if owner is None or owner._state is None:
            raise RuntimeError("LLM client lifecycle owner has not been started for this event loop")
        state = owner._state
        if state.closing:
            raise RuntimeError("LLM client cache is shutting down on this event loop")

        self._settle_completed_closer(state)
        key = (base_url, api_key, cf_access_client_id, cf_access_client_secret)
        entry = state.bucket.get(key)
        if entry is not None:
            state.bucket.move_to_end(key)
            return entry.client, entry.http_owner
        if state.closer is not None or state.retired or state.failed or state.background_errors:
            raise RuntimeError("LLM client cache cannot create a distinct client while eviction cleanup is pending")

        client = _build_llm_client(*key)
        entry = _LlmClientEntry(client=client, http_owner=AsyncResourceOwner("LLM HTTP requests"))
        state.bucket[key] = entry
        if len(state.bucket) > self.maxsize:
            _, evicted = state.bucket.popitem(last=False)
            evicted.http_owner.close_admission()
            state.retired.append(evicted)
            self._ensure_closer(state)
        return entry.client, entry.http_owner

    def _lookup_owner(self, loop: asyncio.AbstractEventLoop) -> _LlmClientLifecycleOwner | None:
        token = id(loop)
        with self._index_lock:
            current = self._owners.get(token)
            if current is None or current[0]() is not loop:
                return None
            owner = current[1]()
            if owner is None:
                self._owners.pop(token, None)
            return owner

    def _lookup_state(self, loop: asyncio.AbstractEventLoop) -> _LoopClientCacheState | None:
        owner = self._lookup_owner(loop)
        return None if owner is None else owner._state

    def _detach_owner(self, owner: _LlmClientLifecycleOwner, loop: asyncio.AbstractEventLoop) -> None:
        token = id(loop)
        with self._index_lock:
            current = self._owners.get(token)
            if current is not None and current[0]() is loop and current[1]() is owner:
                self._owners.pop(token, None)

    def _settle_completed_closer(self, state: _LoopClientCacheState) -> None:
        closer = state.closer
        if closer is not None and closer.done():
            self._on_closer_done(state, closer)

    def _ensure_closer(self, state: _LoopClientCacheState) -> None:
        self._settle_completed_closer(state)
        if state.closer is not None:
            return
        loop = asyncio.get_running_loop()
        closer = create_lifecycle_task(self._close_retired(state), name="llm-client-cache-closer", loop=loop)
        state.closer = closer
        try:
            closer.add_done_callback(lambda done: self._on_closer_done(state, done))
        except BaseException:
            if not closer.done() and closer.cancelling() == 0:
                closer.cancel()
            raise

    def _on_closer_done(self, state: _LoopClientCacheState, done: asyncio.Task[None]) -> None:
        if state.closer is not done:
            return
        state.closer = None
        try:
            done.result()
        except BaseException as error:  # noqa: BLE001 — retain debt for shutdown
            state.background_errors.append(error)
            if not isinstance(error, asyncio.CancelledError):
                _log.error("LLM client lifecycle task failed", exc_info=(type(error), error, error.__traceback__))

    @staticmethod
    async def _close_entry(entry: _LlmClientEntry) -> None:
        await entry.http_owner.aclose()
        if not entry.client.is_closed():
            await entry.client.close()

    async def _close_retired(self, state: _LoopClientCacheState) -> None:
        failed: list[_LlmClientEntry] = []
        errors: list[BaseException] = []
        while state.retired:
            batch = state.retired
            state.retired = []
            for index, entry in enumerate(batch):
                try:
                    await self._close_entry(entry)
                except BaseException as error:  # noqa: BLE001 — attempt every independent entry
                    current = asyncio.current_task()
                    if isinstance(error, asyncio.CancelledError) and current is not None and current.cancelling():
                        state.failed.extend(failed)
                        state.retired[0:0] = batch[index:]
                        raise
                    failed.append(entry)
                    errors.append(error)
        if errors:
            state.failed.extend(failed)
            if len(errors) == 1:
                raise errors[0]
            raise BaseExceptionGroup("Failed to close cached LLM clients", errors)

    @staticmethod
    def _seal_all_entries(state: _LoopClientCacheState) -> None:
        seen: set[_LlmClientEntry] = set()
        for entries in (state.bucket.values(), state.retired, state.failed):
            for entry in entries:
                if entry not in seen:
                    seen.add(entry)
                    entry.http_owner.close_admission()

    @staticmethod
    def _debt_snapshot(state: _LoopClientCacheState) -> LlmClientCleanupDebt:
        return LlmClientCleanupDebt(
            live=len(state.bucket),
            retired=len(state.retired),
            failed=len(state.failed),
            background_errors=len(state.background_errors),
            closer_pending=state.closer is not None,
            shutdown_pending=state.shutdown_task is not None,
        )

    async def _finalize_state(self, state: _LoopClientCacheState) -> None:
        errors: list[BaseException] = []
        closer = state.closer
        if closer is not None and closer is not asyncio.current_task():
            try:
                await join_task(closer)
            except BaseException as error:  # noqa: BLE001 — continue independent closes
                current = asyncio.current_task()
                if isinstance(error, asyncio.CancelledError) and current is not None and current.cancelling():
                    errors.append(error)
                elif state.closer is closer:
                    state.closer = None
                    state.background_errors.append(error)
            finally:
                if state.closer is closer:
                    state.closer = None

        background_errors = state.background_errors
        state.background_errors = []
        errors.extend(background_errors)
        if not background_errors:
            state.retired[0:0] = state.failed
            state.failed = []
        state.retired.extend(state.bucket.values())
        state.bucket.clear()
        try:
            await self._close_retired(state)
        except BaseException as error:  # noqa: BLE001 — preserve all failures
            errors.append(error)
        if errors:
            if len(errors) == 1:
                raise errors[0]
            raise BaseExceptionGroup("Failed to finalize cached LLM clients", errors)

    async def _finalize_and_detach(
        self,
        owner: _LlmClientLifecycleOwner,
        state: _LoopClientCacheState,
    ) -> None:
        await self._finalize_state(state)
        loop = owner._loop
        if loop is not None:
            self._detach_owner(owner, loop)
        owner._state = None
        owner._loop = None
        owner._closed = True

    def _ensure_shutdown_task(
        self,
        owner: _LlmClientLifecycleOwner,
    ) -> asyncio.Task[None]:
        loop = owner._loop
        state = owner._state
        if loop is None or state is None:
            raise RuntimeError("LLM client lifecycle owner is already detached")
        if asyncio.get_running_loop() is not loop:
            raise RuntimeError("LLM shutdown task must be created on its owner loop")
        task = state.shutdown_task
        if task is None:
            state.closing = True
            self._seal_all_entries(state)
            task = create_lifecycle_task(
                self._finalize_and_detach(owner, state),
                name="llm-client-cache-shutdown",
                loop=loop,
            )
            state.shutdown_task = task
        return task

    def _mark_terminal_closed_loop(
        self,
        owner: _LlmClientLifecycleOwner,
    ) -> LlmClientOwnerLoopClosedError:
        with self._index_lock:
            existing = owner._terminal_error
            if existing is not None:
                return existing
            state = owner._state
            if state is None:
                return LlmClientOwnerLoopClosedError(LlmClientCleanupDebt(0, 0, 0, 0, False, False))
            self._seal_all_entries(state)
            error = LlmClientOwnerLoopClosedError(self._debt_snapshot(state))
            owner._terminal_error = error
        _log.error("LLM client cleanup became terminal: %s", error)
        return error

    async def _aclose_on_owner_loop(self, owner: _LlmClientLifecycleOwner) -> None:
        if owner._closed:
            return
        if owner._terminal_error is not None:
            raise owner._terminal_error
        task = self._ensure_shutdown_task(owner)
        try:
            await join_task(task)
        finally:
            state = owner._state
            if state is not None and state.shutdown_task is task and task.done():
                if task.cancelled() or task.exception() is not None:
                    state.shutdown_task = None

    @staticmethod
    def _publish_foreign_result(
        bridge: concurrent.futures.Future[None],
        task: asyncio.Task[None],
    ) -> None:
        if bridge.done():
            return
        try:
            task.result()
        except BaseException as error:  # noqa: BLE001 — transfer exact physical result
            try:
                bridge.set_exception(error)
            except concurrent.futures.InvalidStateError:
                pass
        else:
            try:
                bridge.set_result(None)
            except concurrent.futures.InvalidStateError:
                pass

    async def _await_foreign_shutdown(
        self,
        owner: _LlmClientLifecycleOwner,
        owner_loop: asyncio.AbstractEventLoop,
        bridge: concurrent.futures.Future[None],
    ) -> None:
        wrapped = asyncio.wrap_future(bridge)
        waiter_cancelled: asyncio.CancelledError | None = None
        while not wrapped.done():
            if owner_loop.is_closed():
                error = self._mark_terminal_closed_loop(owner)
                try:
                    bridge.set_exception(error)
                except concurrent.futures.InvalidStateError:
                    pass
                break
            try:
                await asyncio.wait_for(asyncio.shield(wrapped), timeout=0.05)
            except TimeoutError:
                continue
            except asyncio.CancelledError as error:
                if waiter_cancelled is None:
                    waiter_cancelled = error
        wrapped.result()
        if waiter_cancelled is not None:
            raise waiter_cancelled

    async def _aclose_owner(self, owner: _LlmClientLifecycleOwner) -> None:
        if owner._closed:
            return
        if owner._terminal_error is not None:
            raise owner._terminal_error
        owner_loop = owner._loop
        if owner_loop is None:
            return
        if owner_loop.is_closed():
            raise self._mark_terminal_closed_loop(owner)
        if asyncio.get_running_loop() is owner_loop:
            await self._aclose_on_owner_loop(owner)
            return

        bridge: concurrent.futures.Future[None] = concurrent.futures.Future()

        def dispatch() -> None:
            try:
                task = self._ensure_shutdown_task(owner)
            except BaseException as error:  # noqa: BLE001 — bridge owner-loop setup failures
                if not bridge.done():
                    bridge.set_exception(error)
                return
            task.add_done_callback(lambda done: self._publish_foreign_result(bridge, done))

        try:
            owner_loop.call_soon_threadsafe(dispatch)
        except RuntimeError:
            raise self._mark_terminal_closed_loop(owner) from None
        await self._await_foreign_shutdown(owner, owner_loop, bridge)

    async def aclose_current_loop(self) -> None:
        """Close the generation owned by the running loop, if one is active."""
        owner = self._lookup_owner(asyncio.get_running_loop())
        if owner is not None:
            await owner.aclose()


_llm_clients = _LlmClientCache(maxsize=4)


def startup_llm_clients() -> _LlmClientLifecycleOwner:
    """Start and return the current application's strong LLM lifecycle owner."""
    return _llm_clients.startup_current_loop()


async def close_llm_clients(owner: _LlmClientLifecycleOwner | None = None) -> None:
    """Close one explicit generation on its owner loop."""
    if owner is None:
        await _llm_clients.aclose_current_loop()
    else:
        await owner.aclose()


def make_agent_session(
    settings: Settings,
    input_enricher: InputEnricher | None = None,
) -> AgentSession:
    """Create an AgentSession from *settings*.

    Shared factory used by the HTTP API (api/chat.py) so chat and voice
    session rehydration don't duplicate the OpenAI client + session wiring.
    """
    client, http_owner = _llm_clients.get_session_resources(
        settings.llm_base_url,
        settings.llm_api_key,
        settings.cf_access_client_id,
        settings.cf_access_client_secret,
    )
    return AgentSession(
        client=client,
        llm_http_owner=http_owner,
        model=settings.llm_model,
        system_prompt=build_system_prompt(),
        tool_schemas=TOOL_SCHEMAS,
        tool_handlers=TOOL_HANDLERS,
        input_enricher=input_enricher,
        extra_body=settings.llm_extra_body,
        telemetry=configure_telemetry(),
    )
