"""HTTP API for frontend route planning flows."""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from datetime import datetime
from typing import NoReturn

from dotenv import load_dotenv

# Keep env loading before Kiosk planner imports so API and CLI share .env config.
load_dotenv()

import httpx  # noqa: E402
from fastapi import FastAPI, HTTPException, Query, Request, UploadFile  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from openai import OpenAI  # noqa: E402
from pydantic import BaseModel, ConfigDict, Field, field_validator  # noqa: E402

from agent.prompt import build_system_prompt  # noqa: E402
from agent.session import AgentSession, summarize_error  # noqa: E402
from agent.telemetry import configure_telemetry  # noqa: E402
from agent.tools import TOOL_HANDLERS, TOOL_SCHEMAS  # noqa: E402
from tools.kiosk_bus import prefetch_route_arrival_context  # noqa: E402
from tools.kiosk_route_planner import (  # noqa: E402
    InvalidRouteDestination,
    RoutePlanningUnavailable,
    RoutePlanNotFound,
    plan_route_to_coordinate,
    route_plan_to_view_model,
)
from tools.moovo import (  # noqa: E402
    MoovoApiError,
    MoovoConfigError,
    MoovoError,
    MoovoStation,
    NearbyMoovoStation,
    load_moovo_stations,
    nearby_moovo_stations,
)


def _cors_origins() -> list[str]:
    configured = os.getenv("API_CORS_ORIGINS", "")
    return [origin.strip() for origin in configured.split(",") if origin.strip()]


app = FastAPI(title="Taigi Bus Agent API")
cors_origins = _cors_origins()
if cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["*"],
        expose_headers=["*"],
    )

LngLat = tuple[float, float]

# ---------------------------------------------------------------------------
# Chat session management
# ---------------------------------------------------------------------------

_SESSION_TTL_SECONDS = 1800  # 30 min idle expiry

_chat_sessions: dict[str, AgentSession] = {}
_session_last_used: dict[str, float] = {}


def _make_agent_session() -> AgentSession:
    base_url = os.getenv("LLM_BASE_URL")
    model = os.getenv("LLM_MODEL")
    api_key = os.getenv("LLM_API_KEY", "ollama")

    if not base_url or not model:
        raise RuntimeError("LLM_BASE_URL / LLM_MODEL not configured")

    return AgentSession(
        client=OpenAI(base_url=base_url, api_key=api_key),
        model=model,
        system_prompt=build_system_prompt(),
        tool_schemas=TOOL_SCHEMAS,
        tool_handlers=TOOL_HANDLERS,
        input_enricher=prefetch_route_arrival_context,
        telemetry=configure_telemetry(),
    )


def _purge_expired_sessions() -> None:
    now = time.time()
    expired = [
        sid
        for sid, last in _session_last_used.items()
        if now - last > _SESSION_TTL_SECONDS
    ]
    for sid in expired:
        _chat_sessions.pop(sid, None)
        _session_last_used.pop(sid, None)


# ---------------------------------------------------------------------------
# Pydantic models — Route planning
# ---------------------------------------------------------------------------


class DestinationRequest(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)


class RoutePlanRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    destination: DestinationRequest
    departure_time: datetime | None = Field(default=None, alias="departureTime")

    @field_validator("departure_time")
    @classmethod
    def require_departure_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("departureTime must include a timezone")
        return value


class PlaceResponse(BaseModel):
    name: str
    lat: float
    lng: float


class RouteNameResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    short_name: str | None = Field(alias="shortName")
    long_name: str | None = Field(alias="longName")


class LegResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    mode: str
    from_name: str = Field(alias="fromName")
    to_name: str = Field(alias="toName")
    start: datetime
    end: datetime
    duration: float
    distance: float
    coordinates: list[LngLat]
    route: RouteNameResponse | None


class RouteOptionResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str
    coordinates: list[LngLat]
    duration: int
    distance: float
    transfer_count: int = Field(alias="transferCount")
    legs: list[LegResponse]


class RoutePlanResponse(BaseModel):
    origin: PlaceResponse
    destination: PlaceResponse
    routes: list[RouteOptionResponse]


class MoovoStationResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    station_uid: str = Field(alias="stationUid")
    station_id: str | None = Field(alias="stationId")
    name: str
    lat: float
    lng: float
    bike_capacity: int = Field(alias="bikeCapacity")
    available_rent_bikes: int = Field(alias="availableRentBikes")
    available_return_bikes: int = Field(alias="availableReturnBikes")
    service_status: int = Field(alias="serviceStatus")
    update_time: datetime | None = Field(alias="updateTime")


class NearbyMoovoStationResponse(MoovoStationResponse):
    distance_meters: float = Field(alias="distanceMeters")


class MoovoStationsResponse(BaseModel):
    stations: list[MoovoStationResponse]


class NearbyMoovoStationsResponse(BaseModel):
    stations: list[NearbyMoovoStationResponse]


# ---------------------------------------------------------------------------
# Pydantic models — Chat
# ---------------------------------------------------------------------------


class ChatSessionResponse(BaseModel):
    # Intentionally camelCase to match the JSON key the frontend expects.
    sessionId: str  # noqa: N815


class ChatMessageRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)


class ChatMessageResponse(BaseModel):
    reply: str


# ---------------------------------------------------------------------------
# Kiosk info endpoint
# ---------------------------------------------------------------------------


class KioskResponse(BaseModel):
    name: str
    lat: float
    lng: float


@app.get("/api/kiosk", response_model=KioskResponse)
def get_kiosk() -> object:
    """Return the kiosk stop name and its actual OTP origin coordinates."""
    from tools.kiosk_route_planner import _kiosk_place  # noqa: PLC0415

    place = _kiosk_place()
    if place is None:
        stop = os.getenv("KIOSK_STOP", "雲林科技大學")
        raise HTTPException(
            status_code=503, detail=f"找不到站牌「{stop}」的座標資料"
        )
    return KioskResponse(
        name=place.name,
        lat=place.coordinate.latitude,
        lng=place.coordinate.longitude,
    )


# ---------------------------------------------------------------------------
# Route planning endpoint
# ---------------------------------------------------------------------------


@app.post("/api/route-plans", response_model=RoutePlanResponse)
def create_route_plan(request: RoutePlanRequest) -> object:
    """Plan from the configured Kiosk origin to a frontend-selected destination."""
    try:
        plan = plan_route_to_coordinate(
            request.destination.lat,
            request.destination.lng,
            request.departure_time,
        )
    except InvalidRouteDestination as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except RoutePlanNotFound as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except RoutePlanningUnavailable as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    return route_plan_to_view_model(plan)


# ---------------------------------------------------------------------------
# MOOVO station endpoints
# ---------------------------------------------------------------------------


def _moovo_station_response(station: MoovoStation) -> dict[str, object]:
    return {
        "stationUid": station.station_uid,
        "stationId": station.station_id,
        "name": station.name,
        "lat": station.latitude,
        "lng": station.longitude,
        "bikeCapacity": station.bike_capacity,
        "availableRentBikes": station.available_rent_bikes,
        "availableReturnBikes": station.available_return_bikes,
        "serviceStatus": station.service_status,
        "updateTime": station.update_time,
    }


def _nearby_moovo_station_response(item: NearbyMoovoStation) -> dict[str, object]:
    payload = _moovo_station_response(item.station)
    payload["distanceMeters"] = item.distance_meters
    return payload


def _raise_moovo_unavailable(error: MoovoError) -> NoReturn:
    raise HTTPException(status_code=503, detail=str(error)) from error


@app.get("/api/moovo/stations", response_model=MoovoStationsResponse)
def list_moovo_stations() -> object:
    """Return Yunlin MOOVO stations with current TDX availability."""
    try:
        stations = load_moovo_stations()
    except (MoovoApiError, MoovoConfigError) as error:
        _raise_moovo_unavailable(error)
    return {"stations": [_moovo_station_response(station) for station in stations]}


@app.get("/api/moovo/stations/nearby", response_model=NearbyMoovoStationsResponse)
def list_nearby_moovo_stations(
    lat: float = Query(ge=-90, le=90),
    lng: float = Query(ge=-180, le=180),
    radius: int = Query(default=1000, ge=1, le=5000),
    limit: int = Query(default=20, ge=1, le=20),
) -> object:
    """Return Yunlin MOOVO stations near a frontend-selected coordinate."""
    try:
        stations = nearby_moovo_stations(
            lat,
            lng,
            radius_meters=radius,
            limit=limit,
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail="目的地座標格式有誤") from error
    except (MoovoApiError, MoovoConfigError) as error:
        _raise_moovo_unavailable(error)
    return {"stations": [_nearby_moovo_station_response(item) for item in stations]}


# ---------------------------------------------------------------------------
# Chat endpoints
# ---------------------------------------------------------------------------


@app.post("/api/chat/sessions", response_model=ChatSessionResponse)
def create_chat_session() -> object:
    """Create a new agent chat session. Returns a session_id for subsequent messages."""
    _purge_expired_sessions()

    try:
        session = _make_agent_session()
    except RuntimeError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error

    session_id = str(uuid.uuid4())
    _chat_sessions[session_id] = session
    _session_last_used[session_id] = time.time()
    return ChatSessionResponse(sessionId=session_id)


@app.post(
    "/api/chat/sessions/{session_id}/messages",
    response_model=ChatMessageResponse,
)
async def send_chat_message(session_id: str, body: ChatMessageRequest) -> object:
    """Send a message to an existing session. Runs agent in a thread (blocking I/O)."""
    session = _chat_sessions.get(session_id)
    if session is None:
        raise HTTPException(
            status_code=404, detail="對話階段不存在或已過期，請重新開始"
        )

    _session_last_used[session_id] = time.time()

    try:
        reply = await asyncio.to_thread(session.respond, body.message)
    except Exception as error:
        raise HTTPException(
            status_code=500,
            detail=f"助理暫時無法回應：{summarize_error(error)}",
        ) from error

    return {"reply": reply}


@app.delete("/api/chat/sessions/{session_id}", status_code=204)
def delete_chat_session(session_id: str) -> None:
    """Explicitly end a chat session and free its memory."""
    _chat_sessions.pop(session_id, None)
    _session_last_used.pop(session_id, None)


# ---------------------------------------------------------------------------
# ASR endpoint
# ---------------------------------------------------------------------------

_ASR_TIMEOUT_SECONDS = 30
_ASR_MAX_BYTES = 25 * 1024 * 1024  # 25 MB — OpenAI Whisper API 上限


def _asr_config() -> tuple[str, str, str]:
    """Return (base_url, model, api_key) from env, raise 503 if not configured.

    api_key may be empty for self-hosted endpoints that don't require auth.
    """
    base_url = os.getenv("ASR_BASE_URL", "").rstrip("/")
    model = os.getenv("ASR_MODEL", "")
    api_key = os.getenv("ASR_API_KEY", "")
    if not base_url or not model:
        raise HTTPException(
            status_code=503, detail="ASR 服務尚未設定（ASR_BASE_URL / ASR_MODEL）"
        )
    return base_url, model, api_key


async def _asr_post_audio(
    url: str,
    headers: dict[str, str],
    filename: str,
    audio_bytes: bytes,
    content_type: str,
    model: str,
) -> httpx.Response:
    """Send audio bytes to the ASR endpoint. Extracted for testability."""
    async with httpx.AsyncClient(timeout=_ASR_TIMEOUT_SECONDS) as client:
        return await client.post(
            url,
            headers=headers,
            files={"file": (filename, audio_bytes, content_type)},
            data={"model": model},
        )


class TranscriptionResponse(BaseModel):
    text: str


@app.post("/api/asr", response_model=TranscriptionResponse)
async def transcribe_audio(request: Request, file: UploadFile) -> object:
    """Proxy multipart audio to the Qwen3-ASR endpoint and return transcription text.

    Accepts any audio format the upstream model supports (webm/opus, wav, mp3…).
    Content-Length is checked first so oversized uploads are rejected before the
    body is fully buffered. A second byte-count guard catches chunked uploads that
    arrive without a Content-Length header.
    """
    base_url, model, api_key = _asr_config()

    # Fix 2: reject oversized uploads before reading the body into memory.
    # Starlette buffers the entire multipart body before the handler runs, so
    # without this early check a 1 GB file would be fully buffered first.
    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > _ASR_MAX_BYTES:
        raise HTTPException(status_code=413, detail="音訊檔案過大（上限 25 MB）")

    audio_bytes = await file.read()
    if len(audio_bytes) > _ASR_MAX_BYTES:
        # Second guard: chunked upload without Content-Length header.
        raise HTTPException(status_code=413, detail="音訊檔案過大（上限 25 MB）")
    if not audio_bytes:
        raise HTTPException(status_code=400, detail="音訊檔案是空的")

    filename = file.filename or "audio.webm"
    content_type = file.content_type or "audio/webm"

    # Fix 3: api_key may be empty for self-hosted endpoints with no auth.
    headers: dict[str, str] = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    try:
        response = await _asr_post_audio(
            f"{base_url}/v1/audio/transcriptions",
            headers,
            filename,
            audio_bytes,
            content_type,
            model,
        )
    except httpx.TimeoutException as error:
        raise HTTPException(
            status_code=504, detail="語音辨識逾時，請縮短錄音或稍後再試"
        ) from error
    except httpx.RequestError as error:
        raise HTTPException(
            status_code=503, detail=f"無法連線到語音辨識服務：{error}"
        ) from error

    if response.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"語音辨識服務回應錯誤（{response.status_code}）",
        )

    try:
        text: str = response.json().get("text", "").strip()
    except Exception as error:
        raise HTTPException(
            status_code=502, detail="語音辨識服務回應格式錯誤"
        ) from error

    if not text:
        raise HTTPException(status_code=422, detail="未聽清楚，請再說一次")

    return {"text": text}
