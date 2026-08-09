"""Process liveness endpoint used by production deployment checks."""

from fastapi import APIRouter, Response

router = APIRouter()


@router.get("/api/health", include_in_schema=False)
def health(response: Response) -> dict[str, str]:
    """Report that the API process is alive without calling upstream services."""
    response.headers["Cache-Control"] = "no-store"
    return {"status": "ok"}
