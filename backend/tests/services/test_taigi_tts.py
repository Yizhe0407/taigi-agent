import asyncio

import httpx
import pytest

from services import taigi_tts
from services.taigi_tts import TTSConfig, TTSSegmentLimitError, split_tailo, synthesize_segments


def test_split_tailo_rejects_request_fanout():
    with pytest.raises(TTSSegmentLimitError):
        split_tailo("a," * (taigi_tts.TTS_MAX_SEGMENTS + 1))


def test_split_tailo_bounds_upstream_segment_size():
    segments = split_tailo("a" * (taigi_tts.TTS_MAX_SEGMENT_CHARS + 1))

    assert len(segments) == 2
    assert all(len(text) <= taigi_tts.TTS_MAX_SEGMENT_CHARS for text, _ in segments)


def test_synthesize_segments_uses_fixed_worker_pool(monkeypatch):
    active = 0
    max_active = 0

    class FakeClient:
        async def post(self, url, **kwargs):
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.01)
            active -= 1
            return httpx.Response(200, content=kwargs["json"]["input"].encode())

    monkeypatch.setattr(taigi_tts, "get_http_client", lambda: FakeClient())
    segments = [(str(index), 0) for index in range(20)]
    config = TTSConfig("http://tts.local", "model", "voice", "")

    responses = asyncio.run(synthesize_segments(config, segments))

    assert max_active == taigi_tts.TTS_MAX_CONCURRENCY
    assert [response.content.decode() for response in responses if isinstance(response, httpx.Response)] == [str(index) for index in range(20)]
