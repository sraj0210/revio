"""Generic dependency readiness endpoint."""

from typing import Literal

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from revio.ports.persistence import PersistenceReadinessPort

router = APIRouter(tags=["system"])


class ReadinessResponse(BaseModel):
    status: Literal["ready", "not_ready"]


@router.get("/ready", response_model=ReadinessResponse)
async def ready(request: Request) -> ReadinessResponse | JSONResponse:
    persistence: PersistenceReadinessPort | None = request.app.state.persistence
    if persistence is None or await persistence.check_ready():
        return ReadinessResponse(status="ready")
    return JSONResponse(status_code=503, content={"status": "not_ready"})
