"""HTTP API for the Taigi Bus Agent frontend."""

from __future__ import annotations

import os

from dotenv import load_dotenv

# Must precede all domain imports — modules read env vars at import time.
load_dotenv()

from fastapi import FastAPI  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402

from .asr import router as asr_router  # noqa: E402
from .chat import router as chat_router  # noqa: E402
from .moovo import router as moovo_router  # noqa: E402
from .route_plans import router as route_plans_router  # noqa: E402


def _cors_origins() -> list[str]:
    raw = os.getenv("API_CORS_ORIGINS", "")
    return [o.strip() for o in raw.split(",") if o.strip()]


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

app.include_router(chat_router)
app.include_router(route_plans_router)
app.include_router(moovo_router)
app.include_router(asr_router)
