# SPDX-License-Identifier: Apache-2.0
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from src.runtime_logging import SIM_LOG_MODES

router = APIRouter()


class SetSimLogConfigRequest(BaseModel):
    mode: str


@router.get("/sim_log_config")
def get_sim_log_config(request: Request):
    shared_queues = request.app.state.SHARED_QUEUES
    if shared_queues is None:
        return JSONResponse(
            {"status": "error", "message": "Simulation not initialized"},
            status_code=500,
        )

    return JSONResponse(
        {
            "status": "success",
            "mode": shared_queues.get_sim_log_mode(),
            "available_modes": list(SIM_LOG_MODES),
        }
    )


@router.post("/sim_log_config")
def set_sim_log_config(request: Request, body: SetSimLogConfigRequest):
    shared_queues = request.app.state.SHARED_QUEUES
    if shared_queues is None:
        return JSONResponse(
            {"status": "error", "message": "Simulation not initialized"},
            status_code=500,
        )

    mode = (body.mode or "").strip().lower()
    if mode not in SIM_LOG_MODES:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported sim log mode '{body.mode}'. Expected one of: {', '.join(SIM_LOG_MODES)}",
        )

    applied_mode = shared_queues.set_sim_log_mode(mode)
    print(f"[ConfigAPI] Simulator log mode set to {applied_mode}")
    return JSONResponse(
        {
            "status": "success",
            "mode": applied_mode,
            "available_modes": list(SIM_LOG_MODES),
        }
    )
