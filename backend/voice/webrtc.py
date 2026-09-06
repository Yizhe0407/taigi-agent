"""Project-owned WebRTC lifecycle adapters.

Pipecat's SmallWebRTC objects detach event handlers and leave several internal
asyncio tasks unjoined.  These adapters keep the dependency's media behavior
while replacing those lifecycle boundaries with strict, cancellation-safe
ownership.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import time
from collections.abc import Coroutine
from dataclasses import dataclass
from typing import Any

from pipecat.transports.smallwebrtc.connection import (
    DATA_CHANNEL_TIMEOUT_SECS,
    MAX_MESSAGE_QUEUE_SIZE,
    SIGNALLING_TYPE,
    PeerLeftMessage,
    SmallWebRTCConnection,
)

from async_lifecycle import cancel_and_join_task, create_lifecycle_task, join_task

_log = logging.getLogger(__name__)


async def dispatch_event_handlers_strict(
    owner: Any,
    event_name: str,
    *args: Any,
    **kwargs: Any,
) -> None:
    """Run one object's registered handlers inline and propagate every failure."""
    event_handler = owner._event_handlers.get(event_name)
    if event_handler is None:
        return

    for handler in tuple(event_handler.handlers):
        result = handler(owner, *args, **kwargs)
        if inspect.isawaitable(result):
            await result


def clear_event_handlers(owner: Any) -> None:
    """Break callback ownership after a terminal close."""
    for event_handler in owner._event_handlers.values():
        event_handler.handlers.clear()


def _raise_errors(message: str, errors: list[BaseException]) -> None:
    if len(errors) == 1:
        raise errors[0]
    if errors:
        raise BaseExceptionGroup(message, errors)


def _raise_primary_and_cleanup(
    message: str,
    primary: BaseException,
    cleanup: BaseException,
) -> None:
    raise BaseExceptionGroup(message, [primary, cleanup]) from None


def _close_unstarted_coroutine(coroutine: Coroutine[Any, Any, Any]) -> None:
    close = getattr(coroutine, "close", None)
    if close is not None:
        close()


@dataclass(eq=False)
class _OwnedPeerEmitter:
    emitter: Any
    listeners_removed: bool = False


class OwnedSmallWebRTCConnection(SmallWebRTCConnection):
    """SmallWebRTC connection with one permanent close gate and owned tasks.

    The upstream implementation creates idle, renegotiation, connection-timeout,
    and data-channel-timeout tasks.  Several are only cancelled (not joined), and
    the renegotiation task is not retained at all.  This class owns every such
    task, retires completed tasks immediately, and drains the active set before a
    terminal disconnect can complete.  Event handlers run inline so lifecycle
    failures cannot be detached or converted into log-only state.
    """

    def __init__(
        self,
        ice_servers: list[Any] | None = None,
        connection_timeout_secs: int = 60,
    ) -> None:
        # Construction is deliberately split in two.  ``__init__`` creates no
        # RTCPeerConnection, so the process owner can adopt this object before
        # ``construct()`` crosses the dependency's fallible resource boundary.
        # If upstream setup fails halfway through, the already-owned object is
        # still available to the normal cancellation-safe disconnect path.
        self._voice_background_tasks: set[asyncio.Task[Any]] = set()
        self._voice_peer_emitters: dict[int, _OwnedPeerEmitter] = {}
        self._voice_disconnect_waiters: set[asyncio.Task[Any]] = set()
        self._voice_operation_tasks: set[asyncio.Task[Any]] = set()
        self._voice_operation_lock = asyncio.Lock()
        self._voice_renegotiation_task: asyncio.Task[None] | None = None
        self._voice_disconnect_task: asyncio.Task[None] | None = None
        self._voice_peer_generation = 0
        self._voice_closing = False
        self._voice_closed = False
        self._voice_peer_closed = False
        self._voice_peer_resetting = False
        self._voice_peer_left_attempted = True
        self._voice_closed_handlers_delivered = False
        self._voice_constructed = False
        self._voice_construction_started = False
        self._voice_initial_ice_servers = ice_servers
        self._voice_initial_connection_timeout_secs = connection_timeout_secs

        # Defaults make a partially constructed connection physically
        # closeable even if the dependency raises before assigning one of its
        # own fields.
        self._pc: Any | None = None
        self._pc_id = "uninitialized-webrtc-peer"
        self._answer: Any | None = None
        self._connect_invoked = False
        self._track_map: dict[Any, Any] = {}
        self._track_getters: dict[Any, Any] = {}
        self._data_channel: Any | None = None
        self._renegotiation_in_progress = False
        self._last_received_time: float | None = None
        self._outgoing_messages_queue: list[Any] = []
        self._data_channel_enabled = False
        self._pending_app_messages: list[Any] = []
        self._connecting_timeout_task: asyncio.Task[Any] | None = None
        self._data_channel_timeout_task: asyncio.Task[Any] | None = None
        self._event_handlers: dict[str, Any] = {}
        self.connection_timeout_secs = connection_timeout_secs

    def construct(self) -> None:
        """Create the dependency peer only after an outer owner adopts ``self``."""
        if self._voice_closing:
            raise RuntimeError("WebRTC connection is closing")
        if self._voice_constructed or self._voice_construction_started:
            raise RuntimeError("WebRTC connection construction is one-shot")

        self._voice_construction_started = True
        try:
            super().__init__(
                ice_servers=self._voice_initial_ice_servers,
                connection_timeout_secs=self._voice_initial_connection_timeout_secs,
            )
        except BaseException:
            # Keep every partially initialized field intact for disconnect().
            raise
        else:
            self._voice_constructed = True
            self._voice_initial_ice_servers = None

    @property
    def closing(self) -> bool:
        return self._voice_closing

    @property
    def closed(self) -> bool:
        return self._voice_closed

    def _initialize(self) -> None:
        if self._voice_peer_emitters:
            raise RuntimeError("Cannot initialize a WebRTC peer before retiring the previous peer")
        self._voice_peer_generation += 1
        self._voice_peer_closed = False
        self._voice_closed_handlers_delivered = False
        self._voice_peer_left_attempted = False
        super()._initialize()

    def _register_peer_emitter(self, emitter: Any) -> None:
        identity = id(emitter)
        existing = self._voice_peer_emitters.get(identity)
        if existing is not None and existing.emitter is emitter:
            return
        self._voice_peer_emitters[identity] = _OwnedPeerEmitter(emitter)

    def _owns_peer_emitter(self, emitter: Any) -> bool:
        owned = self._voice_peer_emitters.get(id(emitter))
        return owned is not None and owned.emitter is emitter

    def _peer_callback_allowed(self, peer: Any, generation: int) -> bool:
        return (
            not self._voice_closing
            and not self._voice_peer_resetting
            and not self._voice_peer_closed
            and self._voice_peer_generation == generation
            and self._pc is peer
        )

    def _channel_callback_allowed(self, peer: Any, generation: int, channel: Any) -> bool:
        return self._peer_callback_allowed(peer, generation) and self._data_channel is channel

    def _setup_listeners(self) -> None:
        """Install generation-scoped listeners owned by the connection teardown."""
        peer = self._pc
        if peer is None:
            raise RuntimeError("Cannot install WebRTC listeners without a peer")
        generation = self._voice_peer_generation
        self._register_peer_emitter(peer)

        @peer.on("datachannel")
        def on_datachannel(channel: Any) -> None:
            if not self._peer_callback_allowed(peer, generation):
                return
            if self._data_channel is not None and self._data_channel is not channel:
                # One peer owns one application channel. Rejecting extras keeps
                # listener and callback ownership bounded for the peer lifetime.
                channel.close()
                return

            self._data_channel = channel
            self._register_peer_emitter(channel)

            @channel.on("open")
            async def on_open() -> None:
                if not self._channel_callback_allowed(peer, generation, channel):
                    return
                _log.debug("WebRTC data channel is open")
                self._flush_message_queue()

            @channel.on("message")
            async def on_message(message: Any) -> None:
                if not self._channel_callback_allowed(peer, generation, channel):
                    return

                if isinstance(message, str) and message.startswith("ping"):
                    self._last_received_time = time.time()
                    return

                try:
                    parsed = json.loads(message)
                    message_type = parsed["type"]
                    signalling_message = parsed.get("message")
                    if message_type == SIGNALLING_TYPE and signalling_message:
                        self._handle_signalling_message(signalling_message)
                        return
                except (KeyError, TypeError, ValueError) as error:
                    _log.error("Error parsing WebRTC JSON message %r: %s", message, error)
                    return

                if not self._channel_callback_allowed(peer, generation, channel):
                    return
                if self.is_connected():
                    # Handler failures deliberately escape to pyee's error channel;
                    # they are application failures, not JSON parsing failures.
                    await self._call_event_handler("app-message", parsed)
                    return

                if len(self._pending_app_messages) >= MAX_MESSAGE_QUEUE_SIZE:
                    _log.warning(
                        "WebRTC pending app-message queue is full (%d); dropping message",
                        MAX_MESSAGE_QUEUE_SIZE,
                    )
                    return
                self._pending_app_messages.append(parsed)

            @channel.on("close")
            async def on_close() -> None:
                if self._channel_callback_allowed(peer, generation, channel):
                    self._data_channel = None
                    self._data_channel_enabled = False
                    self._outgoing_messages_queue.clear()
                await self._retire_peer_emitter(channel)

        @peer.on("connectionstatechange")
        async def on_connectionstatechange() -> None:
            await self._handle_owned_connection_state(peer, generation)

        @peer.on("iceconnectionstatechange")
        async def on_iceconnectionstatechange() -> None:
            if not self._peer_callback_allowed(peer, generation):
                return
            _log.debug(
                "ICE connection state is %s, connection is %s",
                peer.iceConnectionState,
                peer.connectionState,
            )

        @peer.on("icegatheringstatechange")
        async def on_icegatheringstatechange() -> None:
            if not self._peer_callback_allowed(peer, generation):
                return
            _log.debug("ICE gathering state is %s", peer.iceGatheringState)

        @peer.on("track")
        async def on_track(track: Any) -> None:
            if not self._peer_callback_allowed(peer, generation):
                return
            self._register_peer_emitter(track)
            _log.debug("WebRTC %s track received", track.kind)
            await self._call_event_handler("track-started", track)
            if not self._peer_callback_allowed(peer, generation):
                return

            @track.on("ended")
            async def on_ended() -> None:
                if not self._peer_callback_allowed(peer, generation) or not self._owns_peer_emitter(track):
                    return
                _log.debug("WebRTC %s track ended", track.kind)
                try:
                    await self._call_event_handler("track-ended", track)
                except BaseException as primary:
                    try:
                        await self._retire_ended_track(track)
                    except BaseException as cleanup:
                        _raise_primary_and_cleanup(
                            "WebRTC track-ended handler and resource retirement both failed",
                            primary,
                            cleanup,
                        )
                    raise
                await self._retire_ended_track(track)

    async def _handle_owned_connection_state(self, peer: Any, generation: int) -> None:
        if not self._peer_callback_allowed(peer, generation):
            return

        state = peer.connectionState
        if state == "connecting":
            self._monitoring_connecting_state()
        else:
            self._cancel_monitoring_connecting_state()

        if not self._peer_callback_allowed(peer, generation):
            return
        if state == "connected" and not self._data_channel_timeout_task:
            self._start_data_channel_timeout()
        if state == "connected" and not self._connect_invoked:
            return

        _log.debug("WebRTC connection state changed to %s", state)
        await self._call_event_handler(state)
        if state == "closed":
            return
        if state == "failed" and self._peer_callback_allowed(peer, generation):
            _log.warning("WebRTC connection failed; closing peer connection")
            await self.disconnect()

    def add_event_handler(self, event_name: str, handler: Any) -> None:
        if self._voice_closing:
            raise RuntimeError("WebRTC connection is closing")
        super().add_event_handler(event_name, handler)

    async def _call_event_handler(self, event_name: str, *args: Any, **kwargs: Any) -> None:
        if self._voice_closing and event_name != "closed":
            return
        if event_name == "closed":
            # aiortc emits connection-state callbacks in emitter-owned tasks. A
            # physical peer close therefore only *requests* the authoritative
            # disconnect transaction; terminal callbacks are delivered by that
            # transaction after every resource checkpoint has succeeded.
            await self.disconnect()
            return
        await dispatch_event_handlers_strict(self, event_name, *args, **kwargs)

    async def _deliver_closed_handlers(self) -> None:
        """Deliver terminal callbacks once, retaining only unfinished callbacks.

        A closed handler can update an outer resource owner.  Removing all
        handlers in a ``finally`` block would erase that ownership transition if
        one handler failed.  Closed is a one-shot event, so retire each handler
        only after it returns successfully and leave the failed suffix available
        to the next disconnect attempt.
        """
        if self._voice_closed_handlers_delivered:
            return

        event_handler = self._event_handlers.get("closed")
        if event_handler is not None:
            while event_handler.handlers:
                handler = event_handler.handlers[0]
                result = handler(self)
                if inspect.isawaitable(result):
                    await result
                if event_handler.handlers and event_handler.handlers[0] is handler:
                    del event_handler.handlers[0]
                else:
                    for index, registered in enumerate(event_handler.handlers):
                        if registered is handler:
                            del event_handler.handlers[index]
                            break

        self._voice_closed_handlers_delivered = True

    def _record_background_completion(
        self,
        task: asyncio.Task[Any],
        attribute: str | None,
    ) -> None:
        if task.cancelled():
            self._retire_background_task(task, attribute)
            return
        error = task.exception()
        if error is None:
            self._retire_background_task(task, attribute)
            return
        _log.error(
            "WebRTC background task %s failed",
            task.get_name(),
            exc_info=error,
        )

    def _retire_background_task(
        self,
        task: asyncio.Task[Any],
        attribute: str | None = None,
    ) -> None:
        self._voice_background_tasks.discard(task)
        if attribute is not None and getattr(self, attribute, None) is task:
            setattr(self, attribute, None)

    def _has_active_background_task(self, attribute: str) -> bool:
        """Settle one completed slot before deciding whether it can be replaced."""
        task = getattr(self, attribute, None)
        if task is None:
            return False
        if not task.done():
            return True
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        finally:
            self._retire_background_task(task, attribute)
        return False

    def _create_background_task(
        self,
        coroutine: Coroutine[Any, Any, Any],
        *,
        name: str,
        attribute: str | None = None,
    ) -> asyncio.Task[Any]:
        if self._voice_closing or self._voice_peer_resetting:
            _close_unstarted_coroutine(coroutine)
            raise RuntimeError("WebRTC connection is closing")

        task = create_lifecycle_task(coroutine, name=name)

        self._voice_background_tasks.add(task)
        if attribute is not None:
            setattr(self, attribute, task)
        try:
            task.add_done_callback(
                lambda completed, owned_attribute=attribute: self._record_background_completion(
                    completed,
                    owned_attribute,
                )
            )
        except BaseException:
            if not task.done() and task.cancelling() == 0:
                task.cancel()
            raise
        return task

    def _transfer_current_emitter_waiter(self) -> asyncio.Task[Any] | None:
        """Move a callback that awaits disconnect out of the peer being drained."""
        task = asyncio.current_task()
        if task is None:
            return None
        for owned in self._voice_peer_emitters.values():
            waiting = getattr(owned.emitter, "_waiting", None)
            if waiting is not None and task in waiting:
                waiting.discard(task)
                self._voice_disconnect_waiters.add(task)
                return task
        return None

    def _transfer_current_operation_waiter(self) -> asyncio.Task[Any] | None:
        """Exclude an operation that synchronously requests its own disconnect."""
        task = asyncio.current_task()
        if task is None or task not in self._voice_operation_tasks:
            return None
        self._voice_operation_tasks.discard(task)
        self._voice_disconnect_waiters.add(task)
        return task

    async def _execute_serialized_operation[T](
        self,
        coroutine: Coroutine[Any, Any, T],
    ) -> T:
        started = False
        try:
            async with self._voice_operation_lock:
                if self._voice_closing:
                    raise RuntimeError("WebRTC connection is closing")
                started = True
                return await coroutine
        finally:
            if not started:
                _close_unstarted_coroutine(coroutine)

    async def _run_owned_operation[T](
        self,
        coroutine: Coroutine[Any, Any, T],
        *,
        name: str,
    ) -> T:
        """Serialize one peer mutation and keep it owned across caller cancellation."""
        if self._voice_closing or self._voice_peer_resetting:
            _close_unstarted_coroutine(coroutine)
            raise RuntimeError("WebRTC connection is closing")

        serialized = self._execute_serialized_operation(coroutine)
        try:
            task = create_lifecycle_task(serialized, name=name)
        except BaseException:
            _close_unstarted_coroutine(coroutine)
            raise
        self._voice_operation_tasks.add(task)

        try:
            return await join_task(task)
        finally:
            if task.done():
                self._voice_operation_tasks.discard(task)

    async def _drain_operation_tasks(self) -> None:
        """Cancel and join every peer mutation before physical resource teardown."""
        errors: list[BaseException] = []
        for task in tuple(self._voice_operation_tasks):
            try:
                await cancel_and_join_task(task)
            except BaseException as error:  # noqa: BLE001 - settle every operation
                errors.append(error)
            finally:
                if task.done():
                    self._voice_operation_tasks.discard(task)
        _raise_errors("WebRTC operation teardown failed", errors)

    def _monitoring_connecting_state(self) -> None:
        if self._has_active_background_task("_connecting_timeout_task"):
            return

        async def timeout_handler() -> None:
            await asyncio.sleep(self.connection_timeout_secs)
            if self._voice_closing:
                return
            _log.warning("Timeout establishing the WebRTC peer connection; closing it")
            # Request the authoritative close without waiting from inside the
            # timeout task.  The disconnect task is strongly retained until it
            # succeeds or an external owner observes and retries its failure;
            # meanwhile close can cancel/join this timeout without self-deadlock.
            self._request_disconnect()

        self._create_background_task(
            timeout_handler(),
            name=f"webrtc-connecting-timeout-{self.pc_id}",
            attribute="_connecting_timeout_task",
        )

    def _cancel_monitoring_connecting_state(self) -> None:
        task = self._connecting_timeout_task
        if task is not None and not task.done() and task.cancelling() == 0:
            task.cancel()

    def _start_data_channel_timeout(self) -> None:
        if self._has_active_background_task("_data_channel_timeout_task"):
            return

        async def timeout_handler() -> None:
            await asyncio.sleep(DATA_CHANNEL_TIMEOUT_SECS)
            if self._voice_closing:
                return
            if not self._data_channel or self._data_channel.readyState != "open":
                _log.warning("WebRTC data channel did not open before its protocol deadline; dropping the bounded pending-message queue")
                self._outgoing_messages_queue.clear()
                self._data_channel_enabled = False

        self._create_background_task(
            timeout_handler(),
            name=f"webrtc-data-channel-timeout-{self.pc_id}",
            attribute="_data_channel_timeout_task",
        )

    def _cancel_data_channel_timeout(self) -> None:
        task = self._data_channel_timeout_task
        if task is not None and not task.done() and task.cancelling() == 0:
            task.cancel()

    async def _drain_background_tasks(self) -> None:
        errors: list[BaseException] = []
        for task in tuple(self._voice_background_tasks):
            try:
                await cancel_and_join_task(task)
            except BaseException as error:  # noqa: BLE001 - settle every independent task
                errors.append(error)
            finally:
                if task.done():
                    matched_attribute: str | None = None
                    for attribute in (
                        "_connecting_timeout_task",
                        "_data_channel_timeout_task",
                        "_voice_renegotiation_task",
                    ):
                        if getattr(self, attribute, None) is task:
                            matched_attribute = attribute
                            break
                    self._retire_background_task(task, matched_attribute)
        _raise_errors("WebRTC background task teardown failed", errors)

    async def _retire_peer_emitter(self, emitter: Any) -> None:
        """Retire one emitter through explicit listener and callback checkpoints."""
        identity = id(emitter)
        owned = self._voice_peer_emitters.get(identity)
        if owned is None or owned.emitter is not emitter:
            return

        errors: list[BaseException] = []
        current = asyncio.current_task()

        if not owned.listeners_removed:
            try:
                emitter.remove_all_listeners()
            except BaseException as error:  # noqa: BLE001 - retry the emitter on next close
                errors.append(error)
            else:
                owned.listeners_removed = True

        waiting = getattr(emitter, "_waiting", None)
        if waiting is not None:
            for task in tuple(waiting):
                if task is current or task in self._voice_disconnect_waiters:
                    waiting.discard(task)
                    continue
                try:
                    await cancel_and_join_task(task)
                except BaseException as error:  # noqa: BLE001 - settle every callback
                    errors.append(error)
                finally:
                    if task.done():
                        waiting.discard(task)

        has_unsettled_callback = waiting is not None and any(
            not task.done() and task is not current and task not in self._voice_disconnect_waiters for task in waiting
        )
        if owned.listeners_removed and not has_unsettled_callback:
            if self._voice_peer_emitters.get(identity) is owned:
                self._voice_peer_emitters.pop(identity, None)

        _raise_errors("WebRTC emitter teardown failed", errors)

    async def _retire_peer_emitters(self) -> None:
        """Detach listeners and settle every already-scheduled emitter callback."""
        errors: list[BaseException] = []
        for owned in tuple(self._voice_peer_emitters.values()):
            try:
                await self._retire_peer_emitter(owned.emitter)
            except BaseException as error:  # noqa: BLE001 - settle every emitter
                errors.append(error)

        _raise_errors("WebRTC emitter teardown failed", errors)

    async def _close_track_entries(self, entries: tuple[tuple[Any, Any], ...]) -> None:
        errors: list[BaseException] = []
        tracks_by_identity: dict[int, tuple[Any, list[Any]]] = {}
        for key, track in entries:
            if track is None:
                self._track_map.pop(key, None)
                continue
            identity = id(track)
            existing = tracks_by_identity.get(identity)
            if existing is None:
                tracks_by_identity[identity] = (track, [key])
            else:
                existing[1].append(key)

        for track, keys in tracks_by_identity.values():
            idle_task = getattr(track, "_idle_task", None)
            stopped = False
            try:
                track.stop()
            except BaseException as error:  # noqa: BLE001 - stop every independent track
                errors.append(error)
            else:
                stopped = True

            if idle_task is not None:
                try:
                    await cancel_and_join_task(idle_task)
                except BaseException as error:  # noqa: BLE001
                    errors.append(error)

            if stopped:
                for key in keys:
                    if self._track_map.get(key) is track:
                        self._track_map.pop(key, None)
        _raise_errors("WebRTC track teardown failed", errors)

    async def _retire_ended_track(self, raw_track: Any) -> None:
        errors: list[BaseException] = []
        matching_entries = tuple(
            (key, track) for key, track in tuple(self._track_map.items()) if track is raw_track or getattr(track, "_track", None) is raw_track
        )
        try:
            await self._close_track_entries(matching_entries)
        except BaseException as error:  # noqa: BLE001
            errors.append(error)
        try:
            await self._retire_peer_emitter(raw_track)
        except BaseException as error:  # noqa: BLE001
            errors.append(error)

        _raise_errors("WebRTC ended-track retirement failed", errors)

    async def _close_track_tasks(self) -> None:
        await self._close_track_entries(tuple(self._track_map.items()))

    async def _close_peer(self, *, drain_operations: bool = True) -> None:
        errors: list[BaseException] = []

        if drain_operations:
            try:
                await self._drain_operation_tasks()
            except BaseException as error:  # noqa: BLE001
                errors.append(error)

        try:
            await self._retire_peer_emitters()
        except BaseException as error:  # noqa: BLE001
            errors.append(error)

        try:
            await self._close_track_tasks()
        except BaseException as error:  # noqa: BLE001
            errors.append(error)

        try:
            await self._drain_background_tasks()
        except BaseException as error:  # noqa: BLE001
            errors.append(error)

        if not self._voice_peer_closed:
            try:
                if self._pc is not None:
                    await self._pc.close()
            except BaseException as error:  # noqa: BLE001
                errors.append(error)
                self._voice_peer_closed = getattr(self._pc, "connectionState", None) == "closed"
            else:
                self._voice_peer_closed = True

        self._outgoing_messages_queue.clear()
        self._pending_app_messages.clear()
        self._data_channel_enabled = False
        if not self._voice_peer_emitters:
            self._data_channel = None

        _raise_errors("WebRTC peer teardown failed", errors)

    def _retire_terminal_peer_references(self) -> None:
        """Release closed-generation references without weakening retry debt.

        Physical peer teardown and terminal handler delivery are separate
        checkpoints. A failed terminal handler must remain retryable, but it
        must not keep the already-closed aiortc peer, SDP answer, or bound track
        getter cycle alive. Peer restart does not call this terminal-only step.
        """
        if not self._voice_peer_closed:
            raise RuntimeError("Cannot retire an open WebRTC peer")
        self._pc = None
        self._answer = None
        self._track_getters.clear()
        if not self._voice_peer_emitters:
            self._data_channel = None
        self._last_received_time = None
        self._connect_invoked = False
        self._renegotiation_in_progress = False

    async def _initialize_transaction(self, sdp: str, type: str) -> None:
        try:
            await super().initialize(sdp=sdp, type=type)
        except BaseException as primary:
            if self._voice_closing:
                raise
            try:
                await self.disconnect()
            except BaseException as cleanup:
                _raise_primary_and_cleanup(
                    "WebRTC initialization and rollback both failed",
                    primary,
                    cleanup,
                )
            raise

    async def initialize(self, sdp: str, type: str) -> None:
        await self._run_owned_operation(
            self._initialize_transaction(sdp, type),
            name=f"webrtc-initialize-{self.pc_id}",
        )

    async def _connect_operation(self) -> None:
        self._connect_invoked = True
        if not self.is_connected():
            return

        await self._call_event_handler("connected")
        while self._pending_app_messages and not self._voice_closing:
            message = self._pending_app_messages[0]
            await self._call_event_handler("app-message", message)
            if self._voice_closing or self._voice_peer_resetting:
                return
            if self._pending_app_messages and self._pending_app_messages[0] is message:
                del self._pending_app_messages[0]
            else:
                for index, pending in enumerate(self._pending_app_messages):
                    if pending is message:
                        del self._pending_app_messages[index]
                        break

        if self._voice_closing or self._voice_peer_resetting:
            return
        video_input_track = self.video_input_track()
        if video_input_track:
            await video_input_track.discard_old_frames()
        if self._voice_closing or self._voice_peer_resetting:
            return
        screen_video_input_track = self.screen_video_input_track()
        if screen_video_input_track:
            await screen_video_input_track.discard_old_frames()
        if self._voice_closing or self._voice_peer_resetting:
            return
        if video_input_track or screen_video_input_track:
            self.ask_to_renegotiate()

    async def connect(self) -> None:
        await self._run_owned_operation(
            self._connect_operation(),
            name=f"webrtc-connect-{self.pc_id}",
        )

    async def add_ice_candidate(self, candidate: Any) -> None:
        await self._run_owned_operation(
            super().add_ice_candidate(candidate),
            name=f"webrtc-ice-candidate-{self.pc_id}",
        )

    def send_app_message(self, message: Any) -> None:
        if self._voice_closing or self._voice_peer_resetting:
            return
        super().send_app_message(message)

    def ask_to_renegotiate(self) -> None:
        if self._voice_closing or self._voice_peer_resetting:
            return
        super().ask_to_renegotiate()

    async def _renegotiate_transaction(
        self,
        sdp: str,
        type: str,
        restart_pc: bool,
    ) -> None:
        try:
            await self._renegotiate_operation(sdp, type, restart_pc)
        except BaseException as primary:
            if self._voice_closing:
                raise
            try:
                await self.disconnect()
            except BaseException as cleanup:
                _raise_primary_and_cleanup(
                    "WebRTC renegotiation and rollback both failed",
                    primary,
                    cleanup,
                )
            raise

    async def _renegotiate_operation(
        self,
        sdp: str,
        type: str,
        restart_pc: bool,
    ) -> None:
        if restart_pc:
            self._voice_peer_resetting = True
            try:
                reset_errors: list[BaseException] = []
                try:
                    await dispatch_event_handlers_strict(self, "disconnected")
                except BaseException as error:  # noqa: BLE001 - still close the old peer
                    reset_errors.append(error)
                try:
                    # This serialized operation owns the restart. Terminal close
                    # drains the operation set; a peer-generation swap must not
                    # cancel or join the task that is performing the swap itself.
                    await self._close_peer(drain_operations=False)
                except BaseException as error:  # noqa: BLE001 - aggregate independent teardown
                    reset_errors.append(error)
                _raise_errors("WebRTC peer restart teardown failed", reset_errors)
                self._initialize()
            finally:
                self._voice_peer_resetting = False

        await self._create_answer(sdp, type)

        previous = self._voice_renegotiation_task
        if previous is not None:
            try:
                await cancel_and_join_task(previous)
            finally:
                if previous.done():
                    self._retire_background_task(previous, "_voice_renegotiation_task")

        async def finish_renegotiation() -> None:
            await asyncio.sleep(2)
            if not self._voice_closing:
                self._renegotiation_in_progress = False

        self._voice_renegotiation_task = self._create_background_task(
            finish_renegotiation(),
            name=f"webrtc-renegotiation-{self.pc_id}",
            attribute="_voice_renegotiation_task",
        )

    async def renegotiate(self, sdp: str, type: str, restart_pc: bool = False) -> None:
        await self._run_owned_operation(
            self._renegotiate_transaction(sdp, type, restart_pc),
            name=f"webrtc-renegotiate-{self.pc_id}",
        )

    async def _close(self) -> None:
        await self.disconnect()

    def _record_disconnect_completion(self, task: asyncio.Task[None]) -> None:
        if task.cancelled():
            # The retained task itself is the authoritative failure checkpoint.
            _log.error("WebRTC disconnect task %s was cancelled", task.get_name())
            return

        error = task.exception()
        if error is not None:
            # A background-triggered close has no immediate waiter. Keep the
            # failed task as the authoritative checkpoint so the outer owner can
            # observe the exact failure and retry physical cleanup later.
            _log.error(
                "WebRTC disconnect task %s failed",
                task.get_name(),
                exc_info=error,
            )
            return

        if self._voice_disconnect_task is task:
            self._voice_disconnect_task = None

    def _request_disconnect(self) -> asyncio.Task[None]:
        task = self._voice_disconnect_task
        if task is not None:
            return task

        task = create_lifecycle_task(
            self._run_disconnect(),
            name=f"webrtc-disconnect-{self.pc_id}",
        )
        self._voice_disconnect_task = task
        try:
            task.add_done_callback(self._record_disconnect_completion)
        except BaseException:
            if not task.done() and task.cancelling() == 0:
                task.cancel()
            raise
        return task

    async def _run_disconnect(self) -> None:
        errors: list[BaseException] = []

        if not self._voice_peer_left_attempted:
            self._voice_peer_left_attempted = True
            try:
                super().send_app_message({"type": SIGNALLING_TYPE, "message": PeerLeftMessage().model_dump()})
            except BaseException as error:  # noqa: BLE001
                errors.append(error)

        try:
            await self._close_peer()
        except BaseException as error:  # noqa: BLE001
            errors.append(error)

        resources_closed = self._voice_peer_closed and not self._voice_background_tasks and not self._voice_peer_emitters and not self._track_map
        if self._voice_peer_closed:
            try:
                self._retire_terminal_peer_references()
            except BaseException as error:  # noqa: BLE001
                errors.append(error)

        if not errors and resources_closed:
            try:
                await self._deliver_closed_handlers()
            except BaseException as error:  # noqa: BLE001
                errors.append(error)
            else:
                self._voice_closed = True
                clear_event_handlers(self)

        _raise_errors("WebRTC connection disconnect failed", errors)

    async def disconnect(self) -> None:
        operation_waiter = self._transfer_current_operation_waiter()
        emitter_waiter = self._transfer_current_emitter_waiter()
        try:
            if self._voice_closed:
                return
            self._voice_closing = True

            deferred_error: BaseException | None = None
            task = self._voice_disconnect_task
            if task is not None and task.done():
                try:
                    task.result()
                except BaseException as error:  # noqa: BLE001 - retry after preserving it
                    deferred_error = error
                    if self._voice_disconnect_task is task:
                        self._voice_disconnect_task = None
                    task = None

            if task is None:
                task = self._request_disconnect()

            try:
                await join_task(task)
            except BaseException as current_error:
                if deferred_error is not None:
                    _raise_primary_and_cleanup(
                        "Prior WebRTC disconnect and cleanup retry both failed",
                        deferred_error,
                        current_error,
                    )
                raise
            finally:
                if self._voice_disconnect_task is task and task.done():
                    self._voice_disconnect_task = None
            if deferred_error is not None:
                raise deferred_error
        finally:
            if operation_waiter is not None:
                self._voice_disconnect_waiters.discard(operation_waiter)
            if emitter_waiter is not None:
                self._voice_disconnect_waiters.discard(emitter_waiter)
