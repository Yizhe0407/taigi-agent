"""API test isolation for the process-wide chat-store generation owner."""

import asyncio
from collections.abc import Iterator

import pytest

import api.chat as chat


def _retire_chat_store_generation() -> None:
    runtime = chat._chat_store_runtime
    if runtime is not None and not runtime.closed:
        asyncio.run(chat.close_store())
    runtime = chat._chat_store_runtime
    if runtime is not None and not runtime.closed:
        raise AssertionError("chat-store test generation did not close")
    chat._chat_store_runtime = None


@pytest.fixture(autouse=True)
def _isolate_chat_store_generation() -> Iterator[None]:
    """Start every API test with no installed store and join teardown afterward."""
    _retire_chat_store_generation()
    yield
    _retire_chat_store_generation()
