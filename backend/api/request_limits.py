"""Request-size and per-client rate limits for public API endpoints."""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException, Request

_DEFAULT_BODY_LIMIT = 64 * 1024
_ASR_BODY_LIMIT = 26 * 1024 * 1024
_MAX_RATE_LIMIT_CLIENTS = 2048
_STALE_CLIENT_SECONDS = 600.0


class _BodyTooLarge(Exception):
    pass


class RequestBodyLimitMiddleware:
    """Reject oversized bodies while ASGI chunks are still arriving."""

    def __init__(self, app: Any) -> None:
        self.app = app

    @staticmethod
    def _limit_for_path(path: str) -> int:
        return _ASR_BODY_LIMIT if path == "/api/asr" else _DEFAULT_BODY_LIMIT

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope.get("type") != "http" or scope.get("method") not in {"POST", "PUT", "PATCH"}:
            await self.app(scope, receive, send)
            return

        limit = self._limit_for_path(scope.get("path", ""))
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        raw_length = headers.get(b"content-length")
        if raw_length is not None:
            try:
                if int(raw_length) > limit:
                    await self._send_413(scope, receive, send, limit)
                    return
            except ValueError:
                pass

        received = 0
        response_started = False

        async def limited_receive() -> dict:
            nonlocal received
            message = await receive()
            if message.get("type") == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise _BodyTooLarge
            return message

        async def tracked_send(message: dict) -> None:
            nonlocal response_started
            if message.get("type") == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracked_send)
        except _BodyTooLarge:
            if response_started:
                raise
            await self._send_413(scope, receive, send, limit)

    @staticmethod
    async def _send_413(scope: dict, receive: Any, send: Any, limit: int) -> None:
        payload = json.dumps(
            {"detail": f"請求內容過大（上限 {limit // (1024 * 1024) if limit >= 1024 * 1024 else limit // 1024} {'MB' if limit >= 1024 * 1024 else 'KB'}）"},
            ensure_ascii=False,
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json; charset=utf-8"),
                    (b"content-length", str(len(payload)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": payload})


@dataclass
class _Bucket:
    tokens: float
    updated_at: float


class RateLimit:
    """In-process token bucket suitable for the project's single-worker deployment."""

    def __init__(self, requests: int, per_seconds: float) -> None:
        self._capacity = float(requests)
        self._refill_per_second = requests / per_seconds
        # Ordered by recency of activity (see move_to_end below) so eviction
        # under _prune() drops the least-recently-active client, not just
        # whichever one happened to be inserted first.
        self._buckets: OrderedDict[str, _Bucket] = OrderedDict()
        self._lock = asyncio.Lock()

    async def __call__(self, request: Request) -> None:
        if os.getenv("RATE_LIMIT_ENABLED", "true").strip().lower() in {"false", "0", "no"}:
            return

        client = request.client.host if request.client else "unknown"
        now = time.monotonic()
        async with self._lock:
            self._prune(now)
            bucket = self._buckets.get(client)
            if bucket is None:
                bucket = _Bucket(tokens=self._capacity, updated_at=now)
                self._buckets[client] = bucket
            else:
                elapsed = max(0.0, now - bucket.updated_at)
                bucket.tokens = min(self._capacity, bucket.tokens + elapsed * self._refill_per_second)
                bucket.updated_at = now
            self._buckets.move_to_end(client)

            if bucket.tokens < 1.0:
                retry_after = max(1, int((1.0 - bucket.tokens) / self._refill_per_second) + 1)
                raise HTTPException(
                    status_code=429,
                    detail="請求過於頻繁，請稍後再試",
                    headers={"Retry-After": str(retry_after)},
                )
            bucket.tokens -= 1.0

    def _prune(self, now: float) -> None:
        if len(self._buckets) < _MAX_RATE_LIMIT_CLIENTS:
            return
        stale = [key for key, bucket in self._buckets.items() if now - bucket.updated_at > _STALE_CLIENT_SECONDS]
        for key in stale:
            self._buckets.pop(key, None)
        while len(self._buckets) >= _MAX_RATE_LIMIT_CLIENTS:
            # Least-recently-active entry is at the front (move_to_end keeps
            # active clients at the back) — evict that one, not an arbitrary
            # insertion-order-oldest entry that might still be active.
            self._buckets.popitem(last=False)


TTS_RATE_LIMIT = RateLimit(10, 60.0)
ASR_RATE_LIMIT = RateLimit(20, 60.0)
CHAT_SESSION_RATE_LIMIT = RateLimit(10, 60.0)
CHAT_MESSAGE_RATE_LIMIT = RateLimit(30, 60.0)
VOICE_RATE_LIMIT = RateLimit(10, 60.0)
CLIENT_EVENT_RATE_LIMIT = RateLimit(20, 60.0)
ADMIN_WRITE_RATE_LIMIT = RateLimit(10, 60.0)
# Admin's GET endpoints (kiosk config read, stop catalog) aren't the kiosk's
# own hot path — same permissive allowance as MOOVO_RATE_LIMIT, but its own
# instance so the two features don't share a token bucket.
ADMIN_READ_RATE_LIMIT = RateLimit(30, 60.0)
ROUTE_PLAN_RATE_LIMIT = RateLimit(30, 60.0)
MOOVO_RATE_LIMIT = RateLimit(30, 60.0)
