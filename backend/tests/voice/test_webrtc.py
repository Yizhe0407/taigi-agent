import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest
from pyee.asyncio import AsyncIOEventEmitter

from voice.webrtc import OwnedSmallWebRTCConnection


class _RetryableTrack:
    def __init__(self, *, failures: int = 0) -> None:
        self.failures = failures
        self.stop_calls = 0
        self._idle_task: asyncio.Task[Any] | None = None
        self._track: Any | None = None

    def stop(self) -> None:
        self.stop_calls += 1
        if self.failures:
            self.failures -= 1
            raise RuntimeError("track stop failed")


class _CloseTrackingPeer:
    def __init__(self) -> None:
        self.connectionState = "connected"
        self.close_calls = 0

    async def close(self) -> None:
        self.close_calls += 1
        self.connectionState = "closed"


class _EmitterPeer(AsyncIOEventEmitter):
    def __init__(self, *, state: str = "connected") -> None:
        super().__init__()
        self.connectionState = state
        self.iceConnectionState = "connected"
        self.iceGatheringState = "complete"
        self.close_calls = 0
        self.remove_all_calls = 0

    async def close(self) -> None:
        self.close_calls += 1
        self.connectionState = "closed"

    def remove_all_listeners(self, event: str | None = None) -> None:
        self.remove_all_calls += 1
        super().remove_all_listeners(event)

    def getTransceivers(self) -> list[Any]:
        return []


class _EmitterChannel(AsyncIOEventEmitter):
    def __init__(self, *, state: str = "open") -> None:
        super().__init__()
        self.readyState = state
        self.sent: list[str] = []
        self.close_calls = 0
        self.remove_all_calls = 0

    def send(self, message: str) -> None:
        self.sent.append(message)

    def close(self) -> None:
        self.close_calls += 1
        self.readyState = "closed"
        self.emit("close")

    def remove_all_listeners(self, event: str | None = None) -> None:
        self.remove_all_calls += 1
        super().remove_all_listeners(event)


class _EmitterTrack(AsyncIOEventEmitter):
    def __init__(self) -> None:
        super().__init__()
        self.kind = "audio"
        self.remove_all_calls = 0

    def remove_all_listeners(self, event: str | None = None) -> None:
        self.remove_all_calls += 1
        super().remove_all_listeners(event)


def _build_connection(
    peer: Any,
    tracks: dict[int, _RetryableTrack],
    *,
    closed_handlers: list[Any] | None = None,
) -> OwnedSmallWebRTCConnection:
    connection = object.__new__(OwnedSmallWebRTCConnection)
    raw: Any = connection
    raw._voice_background_tasks = set()
    raw._voice_peer_emitters = {}
    raw._voice_disconnect_waiters = set()
    raw._voice_operation_tasks = set()
    raw._voice_operation_lock = asyncio.Lock()
    raw._voice_renegotiation_task = None
    raw._voice_disconnect_task = None
    raw._voice_peer_generation = 1
    raw._voice_closing = False
    raw._voice_closed = False
    raw._voice_peer_closed = False
    raw._voice_peer_resetting = False
    raw._voice_peer_left_attempted = True
    raw._voice_closed_handlers_delivered = False
    raw._pc = peer
    raw._pc_id = "test-peer"
    raw._answer = object()
    raw._track_map = tracks
    raw._track_getters = {0: connection.audio_input_track}
    raw._outgoing_messages_queue = []
    raw._pending_app_messages = []
    raw._data_channel_enabled = True
    raw._event_handlers = {
        "closed": SimpleNamespace(handlers=list(closed_handlers or [])),
    }
    return connection


def _build_emitter_connection(
    peer: _EmitterPeer,
    *,
    app_handlers: list[Any] | None = None,
    closed_handlers: list[Any] | None = None,
    connect_invoked: bool = True,
) -> OwnedSmallWebRTCConnection:
    connection = _build_connection(peer, {}, closed_handlers=closed_handlers)
    raw: Any = connection
    raw._connect_invoked = connect_invoked
    raw._data_channel = None
    raw._last_received_time = None
    raw._connecting_timeout_task = None
    raw._data_channel_timeout_task = None
    raw._renegotiation_in_progress = False
    raw.connection_timeout_secs = 60
    raw._track_getters = {}
    raw._event_handlers.update(
        {
            "app-message": SimpleNamespace(handlers=list(app_handlers or [])),
            "track-started": SimpleNamespace(handlers=[]),
            "track-ended": SimpleNamespace(handlers=[]),
            "connecting": SimpleNamespace(handlers=[]),
            "connected": SimpleNamespace(handlers=[]),
            "disconnected": SimpleNamespace(handlers=[]),
            "failed": SimpleNamespace(handlers=[]),
            "new": SimpleNamespace(handlers=[]),
        }
    )
    connection._setup_listeners()
    return connection


def test_disconnect_retains_only_failed_track_and_retries_no_completed_cleanup() -> None:
    async def run() -> None:
        failed = _RetryableTrack(failures=1)
        stopped = _RetryableTrack()
        peer = _CloseTrackingPeer()
        connection = _build_connection(
            peer,
            {
                0: failed,
                1: stopped,
                2: stopped,
            },
        )

        with pytest.raises(RuntimeError, match="track stop failed"):
            await connection.disconnect()

        assert not connection.closed
        assert connection._track_map == {0: failed}
        assert failed.stop_calls == 1
        assert stopped.stop_calls == 1, "one physical track must be stopped once by identity"
        assert peer.close_calls == 1
        assert connection._voice_peer_closed

        await connection.disconnect()

        assert connection.closed
        assert connection._track_map == {}
        assert failed.stop_calls == 2
        assert stopped.stop_calls == 1
        assert peer.close_calls == 1, "a successful physical peer close must not be repeated"

    asyncio.run(run())


def test_disconnect_retries_only_the_failed_terminal_handler() -> None:
    async def run() -> None:
        calls = {"completed": 0, "retryable": 0}

        async def completed(_connection: OwnedSmallWebRTCConnection) -> None:
            calls["completed"] += 1

        async def retryable(_connection: OwnedSmallWebRTCConnection) -> None:
            calls["retryable"] += 1
            if calls["retryable"] == 1:
                raise RuntimeError("closed handler failed")

        peer = _CloseTrackingPeer()
        connection = _build_connection(
            peer,
            {},
            closed_handlers=[completed, retryable],
        )

        with pytest.raises(RuntimeError, match="closed handler failed"):
            await connection.disconnect()

        assert not connection.closed
        assert calls == {"completed": 1, "retryable": 1}
        assert connection._event_handlers["closed"].handlers == [retryable]
        assert peer.close_calls == 1
        assert connection._pc is None
        assert connection._answer is None
        assert connection._track_getters == {}

        await connection.disconnect()

        assert connection.closed
        assert calls == {"completed": 1, "retryable": 2}
        assert connection._event_handlers["closed"].handlers == []
        assert peer.close_calls == 1

    asyncio.run(run())


def test_scheduled_message_callback_cannot_mutate_after_close_gate() -> None:
    async def run() -> None:
        peer = _EmitterPeer()
        connection = _build_emitter_connection(peer, connect_invoked=False)
        channel = _EmitterChannel(state="closed")
        peer.emit("datachannel", channel)

        channel.emit("message", json.dumps({"type": "application", "value": 1}))
        callback = next(iter(channel._waiting))

        await connection.disconnect()
        await callback

        assert connection._pending_app_messages == []
        assert connection.closed
        assert channel.complete

    asyncio.run(run())


def test_disconnect_cancels_and_joins_non_waiter_emitter_callback() -> None:
    async def run() -> None:
        started = asyncio.Event()
        cancelled = asyncio.Event()
        never_finishes = asyncio.Event()

        async def app_handler(
            _connection: OwnedSmallWebRTCConnection,
            _message: dict[str, Any],
        ) -> None:
            started.set()
            try:
                await never_finishes.wait()
            finally:
                cancelled.set()

        peer = _EmitterPeer()
        connection = _build_emitter_connection(peer, app_handlers=[app_handler])
        channel = _EmitterChannel()
        peer.emit("datachannel", channel)
        channel.emit("message", json.dumps({"type": "application"}))
        callback = next(iter(channel._waiting))

        await started.wait()
        await connection.disconnect()

        assert callback.done()
        assert callback.cancelled()
        assert cancelled.is_set()
        assert connection._voice_peer_emitters == {}
        assert channel.complete

    asyncio.run(run())


def test_emitter_callback_can_request_authoritative_disconnect_without_deadlock() -> None:
    async def run() -> None:
        returned_from_disconnect = asyncio.Event()

        async def app_handler(
            connection: OwnedSmallWebRTCConnection,
            _message: dict[str, Any],
        ) -> None:
            await connection.disconnect()
            returned_from_disconnect.set()

        peer = _EmitterPeer()
        connection = _build_emitter_connection(peer, app_handlers=[app_handler])
        channel = _EmitterChannel()
        peer.emit("datachannel", channel)
        channel.emit("message", json.dumps({"type": "application"}))
        callback = next(iter(channel._waiting))

        await asyncio.wait_for(returned_from_disconnect.wait(), timeout=1)
        await callback

        assert connection.closed
        assert connection._voice_disconnect_waiters == set()
        assert channel.complete

    asyncio.run(run())


def test_old_generation_message_callback_cannot_mutate_new_peer_state() -> None:
    async def run() -> None:
        old_peer = _EmitterPeer()
        connection = _build_emitter_connection(old_peer, connect_invoked=False)
        old_channel = _EmitterChannel()
        old_peer.emit("datachannel", old_channel)
        old_channel.emit("message", json.dumps({"type": "application", "old": True}))
        old_callback = next(iter(old_channel._waiting))

        new_peer = _EmitterPeer()
        raw: Any = connection
        raw._voice_peer_generation += 1
        raw._pc = new_peer
        raw._data_channel = None

        await old_callback

        assert connection._pending_app_messages == []
        await connection._retire_peer_emitters()
        await old_peer.close()
        await new_peer.close()

    asyncio.run(run())


def test_terminal_teardown_removes_each_listener_owner_once() -> None:
    async def run() -> None:
        peer = _EmitterPeer()
        connection = _build_emitter_connection(peer)
        channel = _EmitterChannel()
        track = _EmitterTrack()
        peer.emit("datachannel", channel)
        peer.emit("track", track)
        track_callback = next(iter(peer._waiting))
        await track_callback

        assert track.listeners("ended")

        await connection.disconnect()
        await connection.disconnect()

        assert peer.remove_all_calls == 1
        assert channel.remove_all_calls == 1
        assert track.remove_all_calls == 1
        assert peer.close_calls == 1
        assert not peer.event_names()
        assert not channel.event_names()
        assert not track.event_names()

    asyncio.run(run())


def test_app_handler_failure_is_not_misreported_as_json_parse_error() -> None:
    async def run() -> None:
        error_seen = asyncio.Event()
        errors: list[BaseException] = []

        async def app_handler(
            _connection: OwnedSmallWebRTCConnection,
            _message: dict[str, Any],
        ) -> None:
            raise RuntimeError("app handler failed")

        peer = _EmitterPeer()
        connection = _build_emitter_connection(peer, app_handlers=[app_handler])
        channel = _EmitterChannel()
        peer.emit("datachannel", channel)

        @channel.on("error")
        def on_error(error: BaseException) -> None:
            errors.append(error)
            error_seen.set()

        channel.emit("message", json.dumps({"type": "application"}))
        callback = next(iter(channel._waiting))

        with pytest.raises(RuntimeError, match="app handler failed"):
            await callback
        await asyncio.wait_for(error_seen.wait(), timeout=1)

        assert len(errors) == 1
        assert isinstance(errors[0], RuntimeError)
        connection._voice_closing = True
        await connection._retire_peer_emitters()
        await peer.close()

    asyncio.run(run())


def test_peer_mutations_are_serialized_by_one_operation_owner() -> None:
    async def run() -> None:
        peer = _EmitterPeer()
        connection = _build_emitter_connection(peer)
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        active = 0
        maximum_active = 0
        calls = 0

        async def create_answer(_sdp: str, _type: str) -> None:
            nonlocal active, maximum_active, calls
            calls += 1
            active += 1
            maximum_active = max(maximum_active, active)
            try:
                if calls == 1:
                    first_started.set()
                    await release_first.wait()
            finally:
                active -= 1

        raw: Any = connection
        raw._create_answer = create_answer

        first = asyncio.create_task(connection.initialize("first", "offer"))
        await first_started.wait()
        second = asyncio.create_task(connection.initialize("second", "offer"))
        await asyncio.sleep(0)

        assert maximum_active == 1
        assert len(connection._voice_operation_tasks) == 2

        release_first.set()
        await first
        await second

        assert calls == 2
        assert maximum_active == 1
        assert connection._voice_operation_tasks == set()
        await connection.disconnect()

    asyncio.run(run())


def test_disconnect_cancels_and_joins_an_inflight_peer_mutation() -> None:
    async def run() -> None:
        peer = _EmitterPeer()
        connection = _build_emitter_connection(peer)
        operation_started = asyncio.Event()
        cancellation_started = asyncio.Event()
        allow_cancellation_cleanup = asyncio.Event()
        cancellation_finished = asyncio.Event()

        async def create_answer(_sdp: str, _type: str) -> None:
            operation_started.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                cancellation_started.set()
                await allow_cancellation_cleanup.wait()
                cancellation_finished.set()
                raise

        raw: Any = connection
        raw._create_answer = create_answer

        initialize = asyncio.create_task(connection.initialize("offer", "offer"))
        await operation_started.wait()
        disconnect = asyncio.create_task(connection.disconnect())
        await cancellation_started.wait()

        assert not disconnect.done()
        assert peer.close_calls == 0

        allow_cancellation_cleanup.set()
        with pytest.raises(asyncio.CancelledError):
            await initialize
        await disconnect

        assert cancellation_finished.is_set()
        assert connection.closed
        assert connection._voice_operation_tasks == set()
        assert peer.close_calls == 1

    asyncio.run(run())


def test_failed_restart_rolls_back_the_new_peer_generation() -> None:
    async def run() -> None:
        old_peer = _EmitterPeer()
        new_peer = _EmitterPeer(state="new")
        connection = _build_emitter_connection(old_peer)
        raw: Any = connection

        def initialize_new_peer() -> None:
            raw._voice_peer_generation += 1
            raw._voice_peer_closed = False
            raw._voice_closed_handlers_delivered = False
            raw._voice_peer_left_attempted = False
            raw._pc = new_peer
            raw._pc_id = "new-peer"
            raw._answer = None
            raw._data_channel = None
            raw._renegotiation_in_progress = False
            raw._last_received_time = None
            raw._outgoing_messages_queue = []
            raw._data_channel_enabled = True
            raw._pending_app_messages = []
            raw._connecting_timeout_task = None
            raw._data_channel_timeout_task = None
            connection._setup_listeners()

        async def fail_answer(_sdp: str, _type: str) -> None:
            raise RuntimeError("new answer failed")

        raw._initialize = initialize_new_peer
        raw._create_answer = fail_answer

        with pytest.raises(RuntimeError, match="new answer failed"):
            await connection.renegotiate("offer", "offer", restart_pc=True)

        assert old_peer.close_calls == 1
        assert new_peer.close_calls == 1
        assert connection.closed
        assert connection._voice_peer_emitters == {}
        assert connection._voice_operation_tasks == set()

    asyncio.run(run())


def test_connect_retires_only_successfully_delivered_pending_messages() -> None:
    async def run() -> None:
        delivered: list[int] = []

        async def app_handler(
            _connection: OwnedSmallWebRTCConnection,
            message: dict[str, Any],
        ) -> None:
            sequence = message["sequence"]
            delivered.append(sequence)
            if sequence == 2:
                raise RuntimeError("delivery failed")

        peer = _EmitterPeer()
        connection = _build_emitter_connection(peer, app_handlers=[app_handler])
        connection._pending_app_messages = [
            {"sequence": 1},
            {"sequence": 2},
            {"sequence": 3},
        ]

        with pytest.raises(RuntimeError, match="delivery failed"):
            await connection.connect()

        assert delivered == [1, 2]
        assert connection._pending_app_messages == [
            {"sequence": 2},
            {"sequence": 3},
        ]
        await connection.disconnect()

    asyncio.run(run())


def test_ended_track_retires_its_emitter_and_idle_task_immediately() -> None:
    async def run() -> None:
        peer = _EmitterPeer()
        connection = _build_emitter_connection(peer)
        raw_track = _EmitterTrack()
        idle_cancelled = asyncio.Event()

        async def idle_watcher() -> None:
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                idle_cancelled.set()
                raise

        peer.emit("track", raw_track)
        started_callback = next(iter(peer._waiting))
        await started_callback

        wrapper = _RetryableTrack()
        wrapper._track = raw_track
        wrapper._idle_task = asyncio.create_task(idle_watcher())
        await asyncio.sleep(0)
        connection._track_map[0] = wrapper

        raw_track.emit("ended")
        ended_callback = next(iter(raw_track._waiting))
        await ended_callback

        assert idle_cancelled.is_set()
        assert wrapper.stop_calls == 1
        assert connection._track_map == {}
        assert not connection._owns_peer_emitter(raw_track)
        assert raw_track.remove_all_calls == 1

        await connection.disconnect()
        assert raw_track.remove_all_calls == 1, "retired track listeners must not be torn down twice"

    asyncio.run(run())


def test_additional_data_channels_are_closed_without_becoming_owned() -> None:
    async def run() -> None:
        peer = _EmitterPeer()
        connection = _build_emitter_connection(peer)
        authoritative = _EmitterChannel()
        rejected = _EmitterChannel()

        peer.emit("datachannel", authoritative)
        peer.emit("datachannel", rejected)

        assert connection._data_channel is authoritative
        assert connection._owns_peer_emitter(authoritative)
        assert not connection._owns_peer_emitter(rejected)
        assert rejected.close_calls == 1
        assert rejected.readyState == "closed"
        assert not rejected.event_names()

        await connection.disconnect()

    asyncio.run(run())


def test_authoritative_data_channel_close_retires_ownership_immediately() -> None:
    async def run() -> None:
        peer = _EmitterPeer()
        connection = _build_emitter_connection(peer)
        channel = _EmitterChannel()
        connection._outgoing_messages_queue.extend(["one", "two"])

        peer.emit("datachannel", channel)
        channel.close()
        close_callback = next(iter(channel._waiting))
        await close_callback

        assert connection._data_channel is None
        assert not connection._data_channel_enabled
        assert connection._outgoing_messages_queue == []
        assert not connection._owns_peer_emitter(channel)
        assert channel.remove_all_calls == 1

        await connection.disconnect()
        assert channel.remove_all_calls == 1, "retired channel listeners must not be torn down twice"

    asyncio.run(run())


def test_background_disconnect_failure_is_retained_observed_and_retried() -> None:
    async def run() -> None:
        failed_track = _RetryableTrack(failures=1)
        peer = _CloseTrackingPeer()
        connection = _build_connection(peer, {0: failed_track})
        connection.connection_timeout_secs = 0
        connection._connecting_timeout_task = None

        connection._monitoring_connecting_state()
        for _ in range(10):
            await asyncio.sleep(0)
            task = connection._voice_disconnect_task
            if task is not None and task.done():
                break

        failed_disconnect = connection._voice_disconnect_task
        assert failed_disconnect is not None
        assert failed_disconnect.done()
        assert failed_disconnect.exception() is not None
        assert connection._track_map == {0: failed_track}
        assert connection._voice_background_tasks == set()
        assert connection._pc is None
        assert connection._answer is None
        assert connection._track_getters == {}

        with pytest.raises(RuntimeError, match="track stop failed"):
            await connection.disconnect()

        assert connection.closed
        assert connection._track_map == {}
        assert failed_track.stop_calls == 2
        assert peer.close_calls == 1
        assert connection._voice_disconnect_task is None

        await connection.disconnect()

    asyncio.run(run())


def test_failed_background_task_is_retained_and_surfaced_once_by_disconnect() -> None:
    async def run() -> None:
        peer = _CloseTrackingPeer()
        connection = _build_connection(peer, {})
        failure = RuntimeError("background lifecycle failed")

        async def fail() -> None:
            raise failure

        task = connection._create_background_task(fail(), name="failed-background")
        while not task.done():
            await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert task in connection._voice_background_tasks

        with pytest.raises(RuntimeError, match="background lifecycle failed") as raised:
            await connection.disconnect()

        assert raised.value is failure
        assert task not in connection._voice_background_tasks
        assert peer.close_calls == 1
        assert not connection.closed

        await connection.disconnect()
        assert connection.closed
        assert peer.close_calls == 1

    asyncio.run(run())
