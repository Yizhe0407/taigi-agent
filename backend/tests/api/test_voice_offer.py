"""POST /api/voice/offer — orphaned peer connection regression coverage.

pipecat's SmallWebRTCRequestHandler swallows any exception raised by the
`webrtc_connection_callback`, registers the connection in its `_pcs_map` and
returns the SDP answer anyway. So when `_start_pipeline` fails before creating
the pipeline task, nobody owns that peer connection and nobody ever closes it.

These tests drive the real handler with a real aiortc-generated offer (ICE
servers disabled so gathering completes instantly) and assert on the handler's
actual connection map rather than on mock calls.
"""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from aiortc import RTCConfiguration, RTCPeerConnection
from fastapi import HTTPException
from pipecat.transports.smallwebrtc.request_handler import SmallWebRTCRequestHandler

import api.voice as voice_module


async def _browser_offer():
    """Return (browser_pc, request_body) for a well-formed local SDP offer."""
    browser = RTCPeerConnection(RTCConfiguration(iceServers=[]))
    browser.createDataChannel("chat")
    browser.addTransceiver("audio", direction="sendrecv")
    offer = await browser.createOffer()
    await browser.setLocalDescription(offer)
    return browser, {"sdp": browser.localDescription.sdp, "type": browser.localDescription.type}


async def _settle(handler: SmallWebRTCRequestHandler, timeout: float = 3.0) -> None:
    """Wait for pipecat's async "closed" handler to evict the pc from _pcs_map."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while handler._pcs_map and loop.time() < deadline:
        await asyncio.sleep(0.01)


class _FakeStore:
    def load_messages(self, _session_id):
        return []

    def create(self):
        return "new-session"


def test_offer_closes_connection_when_session_resolution_fails():
    """Store failure => no pipeline task exists, so the endpoint must close the
    peer connection itself and fail the request instead of handing back an
    answer for a connection nobody owns."""

    async def run():
        browser, body = await _browser_offer()
        handler = SmallWebRTCRequestHandler(ice_servers=[])

        def _broken_store():
            raise RuntimeError("session store unavailable")

        try:
            with (
                patch.object(voice_module, "_handler", handler),
                patch.object(voice_module, "_configured_ice_servers", AsyncMock()),
                patch.object(voice_module, "get_store", _broken_store),
            ):
                with pytest.raises(HTTPException) as excinfo:
                    await voice_module.webrtc_offer(body)
                assert excinfo.value.status_code == 500

            await _settle(handler)
            assert handler._pcs_map == {}, "orphaned peer connection left in pipecat's connection map"
            assert not voice_module._pipeline_tasks
        finally:
            await browser.close()

    asyncio.run(run())


def test_offer_success_path_keeps_connection_and_returns_answer():
    """Control: the normal path is unchanged — an SDP answer comes back and the
    connection stays registered (the pipeline task owns it from here on)."""

    async def run():
        browser, body = await _browser_offer()
        handler = SmallWebRTCRequestHandler(ice_servers=[])
        seen: list[str] = []

        async def _fake_pipeline(_connection, session_id):
            seen.append(session_id)

        try:
            with (
                patch.object(voice_module, "_handler", handler),
                patch.object(voice_module, "_configured_ice_servers", AsyncMock()),
                patch.object(voice_module, "get_store", lambda: _FakeStore()),
                patch("voice.pipeline.run_voice_pipeline", _fake_pipeline),
            ):
                answer = await voice_module.webrtc_offer({**body, "session_id": "chat-42"})

                assert answer["type"] == "answer"
                assert answer["pc_id"] in handler._pcs_map
                # Let the background pipeline task run and deregister itself.
                for _ in range(50):
                    await asyncio.sleep(0.01)
                    if seen:
                        break
                assert seen == ["chat-42"], "frontend-supplied session_id must be reused"

            connection = next(iter(handler._pcs_map.values()))
            await connection.disconnect()
            await _settle(handler)
        finally:
            await browser.close()

    asyncio.run(run())
