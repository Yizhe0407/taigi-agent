import asyncio

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from api.request_limits import RateLimit, RequestBodyLimitMiddleware


def test_rate_limit_rejects_requests_over_capacity(monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "true")
    policy = RateLimit(2, 60)
    request = Request({"type": "http", "method": "POST", "path": "/", "headers": [], "client": ("127.0.0.1", 1)})

    async def run():
        await policy(request)
        await policy(request)
        with pytest.raises(HTTPException) as captured:
            await policy(request)
        return captured.value

    error = asyncio.run(run())
    assert error.status_code == 429
    assert error.headers and "Retry-After" in error.headers


def test_body_limit_rejects_chunked_upload_before_app_finishes(monkeypatch):
    app_completed = False
    sent: list[dict] = []
    messages = iter(
        [
            {"type": "http.request", "body": b"123", "more_body": True},
            {"type": "http.request", "body": b"456", "more_body": False},
        ]
    )

    async def app(scope, receive, send):
        nonlocal app_completed
        while (await receive()).get("more_body"):
            pass
        app_completed = True

    middleware = RequestBodyLimitMiddleware(app)
    monkeypatch.setattr(middleware, "_limit_for_path", lambda path: 5)

    async def receive():
        return next(messages)

    async def send(message):
        sent.append(message)

    asyncio.run(
        middleware(
            {"type": "http", "method": "POST", "path": "/api/asr", "headers": []},
            receive,
            send,
        )
    )

    assert app_completed is False
    assert sent[0]["status"] == 413
