import asyncio
import gc
import threading
import weakref

import httpx
import pytest

from config import (
    LlmClientOwnerLoopClosedError,
    Settings,
    _llm_clients,
    _LlmClientCache,
    parse_cors_origins,
)


class _CloseTrackingClient:
    def __init__(
        self,
        close_started: asyncio.Event | None = None,
        allow_close: asyncio.Event | None = None,
    ) -> None:
        self.close_started = close_started
        self.allow_close = allow_close
        self.closed = False
        self.close_calls = 0

    def is_closed(self) -> bool:
        return self.closed

    async def close(self) -> None:
        self.close_calls += 1
        if self.close_started is not None:
            self.close_started.set()
        if self.allow_close is not None:
            await self.allow_close.wait()
        self.closed = True


class _RetryableCloseClient(_CloseTrackingClient):
    def __init__(self, failures: int) -> None:
        super().__init__()
        self.failures = failures

    async def close(self) -> None:
        self.close_calls += 1
        if self.failures:
            self.failures -= 1
            raise RuntimeError("close failed")
        self.closed = True


class _CleanupBaseError(BaseException):
    pass


class _RetryableBaseCloseClient(_CloseTrackingClient):
    async def close(self) -> None:
        self.close_calls += 1
        if self.close_calls == 1:
            raise _CleanupBaseError("base close failed")
        self.closed = True


class _RetryableCloseStream:
    def __init__(self) -> None:
        self.close_calls = 0
        self.closed = False

    async def aclose(self) -> None:
        self.close_calls += 1
        if self.close_calls == 1:
            raise RuntimeError("stream close failed")
        self.closed = True


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

    async def run() -> None:
        _lifecycle_owner = _llm_clients.startup_current_loop()
        client = _llm_clients.get_session_resources("http://llm.local/v1", "test")[0]

        assert client.max_retries == 0
        assert isinstance(client.timeout, httpx.Timeout)
        assert client.timeout.connect == 5
        assert client.timeout.read == 42
        assert client.timeout.write == 15
        assert client.timeout.pool == 5

        await _llm_clients.aclose_current_loop()
        assert client.is_closed()

    asyncio.run(run())


def test_llm_client_cache_requires_an_owning_event_loop():
    cache = _LlmClientCache(maxsize=1)

    assert not hasattr(cache, "get"), "client-only acquisition would split the generation contract"
    with pytest.raises(RuntimeError, match="owning event loop"):
        cache.get_session_resources("http://llm.local/v1", "test")[0]


def test_llm_client_cache_has_no_loop_wide_request_owner(monkeypatch):
    monkeypatch.setattr("config._build_llm_client", lambda *_args: _CloseTrackingClient())
    cache = _LlmClientCache(maxsize=1)

    async def run() -> None:
        _lifecycle_owner = cache.startup_current_loop()
        _, owner = cache.get_session_resources("http://llm.local/v1", "test")
        state = cache._lookup_state(asyncio.get_running_loop())

        assert state is not None
        assert not hasattr(state, "stream_owner")
        assert next(iter(state.bucket.values())).http_owner is owner
        await cache.aclose_current_loop()

    asyncio.run(run())


def test_llm_client_cache_is_loop_local_and_closes_before_loop_shutdown():
    async def make_one():
        _lifecycle_owner = _llm_clients.startup_current_loop()
        client = _llm_clients.get_session_resources("http://llm.local/v1", "loop-test")[0]
        assert not client.is_closed()
        await _llm_clients.aclose_current_loop()
        return client

    first = asyncio.run(make_one())
    second = asyncio.run(make_one())

    assert first is not second
    assert first.is_closed()
    assert second.is_closed()


def test_llm_client_lru_eviction_closes_evicted_pool(monkeypatch):
    monkeypatch.setattr("config._build_llm_client", lambda *_args: _CloseTrackingClient())
    cache = _LlmClientCache(maxsize=1)

    async def run() -> None:
        _lifecycle_owner = cache.startup_current_loop()
        first, first_owner = cache.get_session_resources("http://llm-1.local/v1", "test")
        second = cache.get_session_resources("http://llm-2.local/v1", "test")[0]
        state = cache._lookup_state(asyncio.get_running_loop())
        assert state is not None
        closer = state.closer
        assert closer is not None

        assert first_owner.closing
        await closer

        assert first.is_closed()
        assert not second.is_closed()

        await cache.aclose_current_loop()
        assert second.is_closed()

    asyncio.run(run())


def test_llm_client_eviction_synchronously_seals_only_the_retired_generation(monkeypatch):
    monkeypatch.setattr("config._build_llm_client", lambda *_args: _CloseTrackingClient())
    cache = _LlmClientCache(maxsize=1)

    async def run() -> None:
        _lifecycle_owner = cache.startup_current_loop()
        _, retired_owner = cache.get_session_resources("http://llm-1.local/v1", "test")
        _, current_owner = cache.get_session_resources("http://llm-2.local/v1", "test")

        assert retired_owner.closing
        assert not current_owner.closing
        with pytest.raises(RuntimeError, match="owner is closed"):
            retired_owner.begin_acquisition()

        acquisition = current_owner.begin_acquisition()
        current_owner.abort_acquisition(acquisition)
        await cache.aclose_current_loop()

    asyncio.run(run())


def test_llm_client_eviction_joins_pending_request_before_parent_close(monkeypatch):
    first = _CloseTrackingClient()
    second = _CloseTrackingClient()
    clients = iter([first, second])
    monkeypatch.setattr("config._build_llm_client", lambda *_args: next(clients))
    cache = _LlmClientCache(maxsize=1)

    async def run() -> None:
        _lifecycle_owner = cache.startup_current_loop()
        _, first_owner = cache.get_session_resources("http://llm-1.local/v1", "test")
        acquisition = first_owner.begin_acquisition()

        cache.get_session_resources("http://llm-2.local/v1", "test")
        state = cache._lookup_state(asyncio.get_running_loop())
        assert state is not None
        closer = state.closer
        assert closer is not None

        await asyncio.sleep(0)
        assert not closer.done()
        assert first.close_calls == 0

        first_owner.abort_acquisition(acquisition)
        await closer

        assert first.closed
        assert first.close_calls == 1
        assert not second.closed
        await cache.aclose_current_loop()

    asyncio.run(run())


def test_llm_client_eviction_joins_active_stream_before_parent_close(monkeypatch):
    first = _CloseTrackingClient()
    second = _CloseTrackingClient()
    clients = iter([first, second])
    monkeypatch.setattr("config._build_llm_client", lambda *_args: next(clients))
    cache = _LlmClientCache(maxsize=1)

    async def run() -> None:
        _lifecycle_owner = cache.startup_current_loop()
        stream_close_started = asyncio.Event()
        allow_stream_close = asyncio.Event()
        _, first_owner = cache.get_session_resources("http://llm-1.local/v1", "test")

        async def close_stream(_stream: object) -> None:
            stream_close_started.set()
            await allow_stream_close.wait()

        first_owner.adopt(object(), close_stream)
        cache.get_session_resources("http://llm-2.local/v1", "test")
        state = cache._lookup_state(asyncio.get_running_loop())
        assert state is not None
        closer = state.closer
        assert closer is not None

        await stream_close_started.wait()
        assert not closer.done()
        assert first.close_calls == 0

        allow_stream_close.set()
        await closer

        assert first.closed
        assert first.close_calls == 1
        assert not second.closed
        await cache.aclose_current_loop()

    asyncio.run(run())


def test_llm_client_cache_applies_backpressure_while_eviction_is_running(monkeypatch):
    async def run() -> None:
        close_started = asyncio.Event()
        allow_close = asyncio.Event()
        first = _CloseTrackingClient(close_started, allow_close)
        second = _CloseTrackingClient()
        clients = iter([first, second])
        build_calls = 0

        def build(*_args):
            nonlocal build_calls
            build_calls += 1
            return next(clients)

        monkeypatch.setattr("config._build_llm_client", build)
        cache = _LlmClientCache(maxsize=1)
        _lifecycle_owner = cache.startup_current_loop()
        cache.get_session_resources("http://llm-1.local/v1", "test")[0]
        assert cache.get_session_resources("http://llm-2.local/v1", "test")[0] is second

        await close_started.wait()
        assert cache.get_session_resources("http://llm-2.local/v1", "test")[0] is second
        with pytest.raises(RuntimeError, match="eviction cleanup is pending"):
            cache.get_session_resources("http://llm-3.local/v1", "test")[0]
        assert build_calls == 2

        allow_close.set()
        await cache.aclose_current_loop()
        assert first.closed
        assert second.closed

    asyncio.run(run())


def test_llm_client_eviction_publishes_closer_before_eager_coroutine_entry(monkeypatch):
    async def run() -> None:
        cache = _LlmClientCache(maxsize=1)
        _lifecycle_owner = cache.startup_current_loop()
        state = cache._lookup_state(asyncio.get_running_loop())
        assert state is not None
        observed: list[bool] = []

        async def inspect_publication(candidate) -> None:
            observed.append(candidate.closer is asyncio.current_task())

        monkeypatch.setattr(cache, "_close_retired", inspect_publication)
        loop = asyncio.get_running_loop()
        original_factory = loop.get_task_factory()
        loop.set_task_factory(asyncio.eager_task_factory)
        try:
            cache._ensure_closer(state)
            closer = state.closer
            assert closer is not None
            await closer
        finally:
            loop.set_task_factory(original_factory)

        assert observed == [True]

    asyncio.run(run())


def test_llm_client_cache_allows_next_miss_only_after_successful_eviction(monkeypatch):
    async def run() -> None:
        clients = [_CloseTrackingClient() for _ in range(3)]
        built = iter(clients)
        monkeypatch.setattr("config._build_llm_client", lambda *_args: next(built))
        cache = _LlmClientCache(maxsize=1)
        _lifecycle_owner = cache.startup_current_loop()

        cache.get_session_resources("http://llm-1.local/v1", "test")[0]
        cache.get_session_resources("http://llm-2.local/v1", "test")[0]
        state = cache._lookup_state(asyncio.get_running_loop())
        assert state is not None
        closer = state.closer
        assert closer is not None
        await closer

        assert cache.get_session_resources("http://llm-3.local/v1", "test")[0] is clients[2]
        await cache.aclose_current_loop()
        assert all(client.closed for client in clients)

    asyncio.run(run())


def test_llm_client_cache_failed_eviction_blocks_new_pool_until_shutdown_retry(monkeypatch):
    async def run() -> None:
        failed = _RetryableCloseClient(failures=1)
        current = _CloseTrackingClient()
        clients = iter([failed, current])
        build_calls = 0

        def build(*_args):
            nonlocal build_calls
            build_calls += 1
            return next(clients)

        monkeypatch.setattr("config._build_llm_client", build)
        cache = _LlmClientCache(maxsize=1)
        _lifecycle_owner = cache.startup_current_loop()
        cache.get_session_resources("http://llm-1.local/v1", "test")[0]
        cache.get_session_resources("http://llm-2.local/v1", "test")[0]
        state = cache._lookup_state(asyncio.get_running_loop())
        assert state is not None
        closer = state.closer
        assert closer is not None
        with pytest.raises(RuntimeError, match="close failed"):
            await closer

        assert cache.get_session_resources("http://llm-2.local/v1", "test")[0] is current
        with pytest.raises(RuntimeError, match="eviction cleanup is pending"):
            cache.get_session_resources("http://llm-3.local/v1", "test")[0]
        assert build_calls == 2

        with pytest.raises(RuntimeError, match="close failed"):
            await cache.aclose_current_loop()
        assert current.closed
        assert not failed.closed

        await cache.aclose_current_loop()
        assert failed.closed
        assert build_calls == 2

    asyncio.run(run())


def test_llm_client_eviction_task_creation_closes_unstarted_coroutine(monkeypatch):
    async def run() -> None:
        first = _CloseTrackingClient()
        second = _CloseTrackingClient()
        clients = iter([first, second])
        monkeypatch.setattr("config._build_llm_client", lambda *_args: next(clients))
        cache = _LlmClientCache(maxsize=1)
        _lifecycle_owner = cache.startup_current_loop()
        _, first_owner = cache.get_session_resources("http://llm-1.local/v1", "test")
        loop = asyncio.get_running_loop()
        created = []

        async def never_started() -> None:
            raise AssertionError("eviction coroutine must not start")

        def build_coroutine(_state):
            coroutine = never_started()
            created.append(coroutine)
            return coroutine

        with monkeypatch.context() as scoped:
            scoped.setattr(cache, "_close_retired", build_coroutine)

            def reject_task(*_args, **_kwargs):
                raise RuntimeError("task creation failed")

            scoped.setattr(loop, "create_task", reject_task)
            with pytest.raises(RuntimeError, match="task creation failed"):
                cache.get_session_resources("http://llm-2.local/v1", "test")[0]

        assert len(created) == 1
        assert created[0].cr_frame is None
        state = cache._lookup_state(loop)
        assert state is not None
        assert state.closer is None
        assert first_owner.closing
        with pytest.raises(RuntimeError, match="owner is closed"):
            first_owner.begin_acquisition()
        assert len(state.retired) == 1
        assert state.retired[0].client is first
        assert state.retired[0].http_owner is first_owner
        assert [entry.client for entry in state.bucket.values()] == [second]

        await cache.aclose_current_loop()
        assert first.closed
        assert second.closed

    asyncio.run(run())


def test_llm_client_shutdown_task_creation_closes_unstarted_coroutine(monkeypatch):
    async def run() -> None:
        client = _CloseTrackingClient()
        monkeypatch.setattr("config._build_llm_client", lambda *_args: client)
        cache = _LlmClientCache(maxsize=1)
        _lifecycle_owner = cache.startup_current_loop()
        loop = asyncio.get_running_loop()
        _, owner = cache.get_session_resources("http://llm.local/v1", "test")
        state = cache._lookup_state(loop)
        assert state is not None
        created = []

        async def never_started() -> None:
            raise AssertionError("shutdown coroutine must not start")

        def build_coroutine(_loop, _state):
            coroutine = never_started()
            created.append(coroutine)
            return coroutine

        with monkeypatch.context() as scoped:
            scoped.setattr(cache, "_finalize_and_detach", build_coroutine)

            def reject_task(*_args, **_kwargs):
                raise RuntimeError("task creation failed")

            scoped.setattr(loop, "create_task", reject_task)
            with pytest.raises(RuntimeError, match="task creation failed"):
                await cache.aclose_current_loop()

        assert len(created) == 1
        assert created[0].cr_frame is None
        assert state.closing
        assert state.shutdown_task is None
        assert owner.closing
        assert not owner.closed
        assert not client.closed
        with pytest.raises(RuntimeError, match="owner is closed"):
            owner.begin_acquisition()

        await cache.aclose_current_loop()
        assert owner.closed
        assert client.closed
        assert cache._lookup_state(loop) is None

    asyncio.run(run())


def test_llm_client_shutdown_joins_in_progress_eviction(monkeypatch):
    async def run() -> None:
        close_started = asyncio.Event()
        allow_close = asyncio.Event()

        monkeypatch.setattr(
            "config._build_llm_client",
            lambda *_args: _CloseTrackingClient(close_started, allow_close),
        )
        cache = _LlmClientCache(maxsize=1)
        _lifecycle_owner = cache.startup_current_loop()
        evicted = cache.get_session_resources("http://llm-1.local/v1", "test")[0]
        current = cache.get_session_resources("http://llm-2.local/v1", "test")[0]

        await close_started.wait()
        shutdown = asyncio.create_task(cache.aclose_current_loop())
        await asyncio.sleep(0)
        assert not shutdown.done()

        allow_close.set()
        await shutdown

        assert evicted.is_closed()
        assert current.is_closed()

    asyncio.run(run())


def test_llm_client_shutdown_rejects_new_acquisition_and_joins_all_waiters(monkeypatch):
    async def run() -> None:
        close_started = asyncio.Event()
        allow_close = asyncio.Event()
        client = _CloseTrackingClient(close_started, allow_close)
        monkeypatch.setattr("config._build_llm_client", lambda *_args: client)
        cache = _LlmClientCache(maxsize=1)
        _lifecycle_owner = cache.startup_current_loop()
        assert cache.get_session_resources("http://llm.local/v1", "test")[0] is client

        first = asyncio.create_task(cache.aclose_current_loop())
        await close_started.wait()
        second = asyncio.create_task(cache.aclose_current_loop())
        await asyncio.sleep(0)

        with pytest.raises(RuntimeError, match="shutting down"):
            cache.get_session_resources("http://replacement.local/v1", "test")[0]
        assert not first.done()
        assert not second.done()

        allow_close.set()
        await asyncio.gather(first, second)

        assert client.closed
        assert client.close_calls == 1

    asyncio.run(run())


def test_llm_client_shutdown_waiter_cancellation_cannot_cancel_physical_close(monkeypatch):
    async def run() -> None:
        close_started = asyncio.Event()
        allow_close = asyncio.Event()
        client = _CloseTrackingClient(close_started, allow_close)
        monkeypatch.setattr("config._build_llm_client", lambda *_args: client)
        cache = _LlmClientCache(maxsize=1)
        _lifecycle_owner = cache.startup_current_loop()
        cache.get_session_resources("http://llm.local/v1", "test")[0]

        waiter = asyncio.create_task(cache.aclose_current_loop())
        await close_started.wait()
        waiter.cancel()
        await asyncio.sleep(0)

        assert not waiter.done()
        assert not client.closed

        allow_close.set()
        with pytest.raises(asyncio.CancelledError):
            await waiter

        assert client.closed
        assert client.close_calls == 1

    asyncio.run(run())


def test_llm_client_failed_child_close_retains_exact_entry_and_closes_unrelated_parent(monkeypatch):
    async def run() -> None:
        failed_parent = _CloseTrackingClient()
        current_parent = _CloseTrackingClient()
        clients = iter([failed_parent, current_parent])
        stream = _RetryableCloseStream()
        monkeypatch.setattr("config._build_llm_client", lambda *_args: next(clients))
        cache = _LlmClientCache(maxsize=1)
        _lifecycle_owner = cache.startup_current_loop()
        _, failed_owner = cache.get_session_resources("http://llm-1.local/v1", "test")
        failed_owner.adopt(stream, lambda candidate: candidate.aclose())
        cache.get_session_resources("http://llm-2.local/v1", "test")
        state = cache._lookup_state(asyncio.get_running_loop())
        assert state is not None
        closer = state.closer
        assert closer is not None

        with pytest.raises(RuntimeError, match="stream close failed"):
            await closer

        assert stream.close_calls == 1
        assert not stream.closed
        assert failed_owner.failed_count == 1
        assert failed_parent.close_calls == 0
        assert not failed_parent.closed
        assert len(state.failed) == 1
        failed_entry = state.failed[0]
        assert failed_entry.client is failed_parent
        assert failed_entry.http_owner is failed_owner

        # The background error is surfaced once. The failed generation is not
        # retried in the same shutdown attempt, but the unrelated live parent is
        # still finalized independently.
        with pytest.raises(RuntimeError, match="stream close failed"):
            await cache.aclose_current_loop()

        assert stream.close_calls == 1
        assert not stream.closed
        assert failed_parent.close_calls == 0
        assert current_parent.closed
        assert current_parent.close_calls == 1
        assert state.failed == [failed_entry]

        await cache.aclose_current_loop()

        assert stream.close_calls == 2
        assert stream.closed
        assert failed_parent.close_calls == 1
        assert failed_parent.closed
        assert current_parent.close_calls == 1
        assert cache._lookup_state(asyncio.get_running_loop()) is None

    asyncio.run(run())


def test_llm_client_shutdown_closes_other_clients_after_existing_closer_failure(monkeypatch):
    async def run() -> None:
        failed = _RetryableCloseClient(failures=1)
        current = _CloseTrackingClient()
        clients = iter([failed, current])
        monkeypatch.setattr("config._build_llm_client", lambda *_args: next(clients))
        cache = _LlmClientCache(maxsize=1)
        _lifecycle_owner = cache.startup_current_loop()
        cache.get_session_resources("http://llm-1.local/v1", "test")[0]
        cache.get_session_resources("http://llm-2.local/v1", "test")[0]

        with pytest.raises(RuntimeError, match="close failed"):
            await cache.aclose_current_loop()

        assert failed.close_calls == 1, "one shutdown attempt retried the same failed owner"
        assert not failed.closed
        assert current.closed
        assert current.close_calls == 1

        await cache.aclose_current_loop()

        assert failed.closed
        assert failed.close_calls == 2
        assert current.close_calls == 1, "a successful close remained in the retry registry"

    asyncio.run(run())


def test_llm_client_shutdown_aggregates_independent_close_failures(monkeypatch):
    async def run() -> None:
        first = _RetryableCloseClient(failures=1)
        second = _RetryableCloseClient(failures=1)
        clients = iter([first, second])
        monkeypatch.setattr("config._build_llm_client", lambda *_args: next(clients))
        cache = _LlmClientCache(maxsize=1)
        _lifecycle_owner = cache.startup_current_loop()
        cache.get_session_resources("http://llm-1.local/v1", "test")[0]
        cache.get_session_resources("http://llm-2.local/v1", "test")[0]

        with pytest.raises(BaseExceptionGroup) as captured:
            await cache.aclose_current_loop()

        assert len(captured.value.exceptions) == 2
        assert first.close_calls == 1
        assert second.close_calls == 1

        await cache.aclose_current_loop()
        assert first.closed
        assert second.closed

    asyncio.run(run())


def test_llm_client_shutdown_retains_base_exception_cleanup_debt(monkeypatch):
    async def run() -> None:
        failed = _RetryableBaseCloseClient()
        current = _CloseTrackingClient()
        clients = iter([failed, current])
        monkeypatch.setattr("config._build_llm_client", lambda *_args: next(clients))
        cache = _LlmClientCache(maxsize=1)
        _lifecycle_owner = cache.startup_current_loop()
        cache.get_session_resources("http://llm-1.local/v1", "test")[0]
        cache.get_session_resources("http://llm-2.local/v1", "test")[0]

        with pytest.raises(_CleanupBaseError, match="base close failed"):
            await cache.aclose_current_loop()

        assert failed.close_calls == 1
        assert not failed.closed
        assert current.closed
        assert current.close_calls == 1

        await cache.aclose_current_loop()

        assert failed.closed
        assert failed.close_calls == 2
        assert current.close_calls == 1

    asyncio.run(run())


def test_process_singleton_weak_index_does_not_pin_abandoned_loop(monkeypatch):
    monkeypatch.setattr("config._build_llm_client", lambda *_args: _CloseTrackingClient())
    cache = _LlmClientCache(maxsize=1)
    loop = asyncio.new_event_loop()

    async def create_generation():
        owner = cache.startup_current_loop()
        client = cache.get_session_resources("http://llm.local/v1", "test")[0]
        return owner, owner._state, client

    owner, state, client = loop.run_until_complete(create_generation())
    loop_ref = weakref.ref(loop)
    owner_ref = weakref.ref(owner)
    state_ref = weakref.ref(state)
    client_ref = weakref.ref(client)
    loop.close()

    del owner, state, client, loop
    gc.collect()

    assert cache._owners == {}
    assert loop_ref() is None
    assert owner_ref() is None
    assert state_ref() is None
    assert client_ref() is None


def test_closed_owner_loop_is_terminal_and_preserves_exact_cleanup_debt(monkeypatch):
    client = _CloseTrackingClient()
    monkeypatch.setattr("config._build_llm_client", lambda *_args: client)
    cache = _LlmClientCache(maxsize=1)
    loop = asyncio.new_event_loop()

    async def create_generation():
        owner = cache.startup_current_loop()
        cache.get_session_resources("http://llm.local/v1", "test")
        return owner

    owner = loop.run_until_complete(create_generation())
    loop.close()

    with pytest.raises(LlmClientOwnerLoopClosedError) as first:
        asyncio.run(owner.aclose())
    with pytest.raises(LlmClientOwnerLoopClosedError) as second:
        asyncio.run(owner.aclose())

    assert first.value is second.value
    assert first.value.debt.live == 1
    assert owner.cleanup_debt is not None
    assert owner.cleanup_debt.live == 1
    assert owner._state is not None
    assert next(iter(owner._state.bucket.values())).client is client
    assert client.close_calls == 0
    assert not client.closed


def test_foreign_loop_shutdown_runs_physical_close_on_owner_loop(monkeypatch):
    owner_loop = asyncio.new_event_loop()
    loop_started = threading.Event()
    owner_thread_id: list[int] = []

    class RecordingClient(_CloseTrackingClient):
        async def close(self) -> None:
            owner_thread_id.append(threading.get_ident())
            assert asyncio.get_running_loop() is owner_loop
            await super().close()

    client = RecordingClient()
    monkeypatch.setattr("config._build_llm_client", lambda *_args: client)
    cache = _LlmClientCache(maxsize=1)

    def run_owner_loop() -> None:
        asyncio.set_event_loop(owner_loop)
        loop_started.set()
        owner_loop.run_forever()

    thread = threading.Thread(target=run_owner_loop)
    thread.start()
    assert loop_started.wait(timeout=2)

    async def create_generation():
        owner = cache.startup_current_loop()
        cache.get_session_resources("http://llm.local/v1", "test")
        return owner

    owner = asyncio.run_coroutine_threadsafe(create_generation(), owner_loop).result(timeout=2)
    asyncio.run(owner.aclose())

    assert client.closed
    assert client.close_calls == 1
    assert owner.closed
    assert owner_thread_id == [thread.ident]
    owner_loop.call_soon_threadsafe(owner_loop.stop)
    thread.join(timeout=2)
    owner_loop.close()


def test_foreign_shutdown_waits_while_stopped_owner_then_closes_after_resume(monkeypatch):
    owner_loop = asyncio.new_event_loop()
    close_thread_id: list[int] = []

    class RecordingClient(_CloseTrackingClient):
        async def close(self) -> None:
            close_thread_id.append(threading.get_ident())
            assert asyncio.get_running_loop() is owner_loop
            await super().close()

    client = RecordingClient()
    monkeypatch.setattr("config._build_llm_client", lambda *_args: client)
    cache = _LlmClientCache(maxsize=1)

    async def create_generation():
        owner = cache.startup_current_loop()
        cache.get_session_resources("http://llm.local/v1", "test")
        return owner

    owner = owner_loop.run_until_complete(create_generation())
    close_result: list[BaseException | None] = []

    def close_from_foreign_loop() -> None:
        try:
            asyncio.run(owner.aclose())
        except BaseException as error:  # pragma: no cover - asserted below
            close_result.append(error)
        else:
            close_result.append(None)

    closer_thread = threading.Thread(target=close_from_foreign_loop)
    closer_thread.start()
    closer_thread.join(timeout=0.1)
    assert closer_thread.is_alive()
    assert owner.cleanup_debt is not None
    assert owner.cleanup_debt.live == 1
    assert not client.closed

    owner_thread = threading.Thread(target=owner_loop.run_forever)
    owner_thread.start()
    closer_thread.join(timeout=2)
    assert not closer_thread.is_alive()
    assert close_result == [None]
    assert client.closed
    assert close_thread_id == [owner_thread.ident]

    owner_loop.call_soon_threadsafe(owner_loop.stop)
    owner_thread.join(timeout=2)
    owner_loop.close()


def test_successful_shutdown_releases_state_client_and_loop(monkeypatch):
    monkeypatch.setattr("config._build_llm_client", lambda *_args: _CloseTrackingClient())
    cache = _LlmClientCache(maxsize=1)
    loop = asyncio.new_event_loop()

    async def lifecycle():
        owner = cache.startup_current_loop()
        client = cache.get_session_resources("http://llm.local/v1", "test")[0]
        state = owner._state
        await owner.aclose()
        return owner, state, client

    owner, state, client = loop.run_until_complete(lifecycle())
    loop_ref = weakref.ref(loop)
    state_ref = weakref.ref(state)
    client_ref = weakref.ref(client)
    assert owner.closed
    assert owner._state is None
    assert cache._owners == {}
    loop.close()

    del owner, state, client, loop
    gc.collect()

    assert loop_ref() is None
    assert state_ref() is None
    assert client_ref() is None
