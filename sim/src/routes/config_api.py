from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, root_validator

# ResetRobotCmd is used by reset_brain; BrainActiveCmd / BrainBackendConfigCmd
# activate/deactivate and configure the brain via rosbridge.
from src.agent.types import (
    BrainActiveCmd,
    BrainBackendConfigCmd,
    ResetRobotCmd,
)
from src.runtime_logging import SIM_LOG_MODES

router = APIRouter()


class ResetBrainRequest(BaseModel):
    memory_state: str | None = None


class SetSimLogConfigRequest(BaseModel):
    mode: str


class BrainBackendConfigRequest(BaseModel):
    websocket_uri: str | None = None
    service_key: str | None = None

    @root_validator(pre=True)
    def check_backend_config_provided(cls, values):
        websocket_uri = (values.get("websocket_uri") or "").strip()
        service_key = (values.get("service_key") or "").strip()
        if not websocket_uri and not service_key:
            raise ValueError("Provide websocket_uri and/or service_key.")
        return values


@router.post("/brain_backend_config")
def set_brain_backend_config(request: Request, body: BrainBackendConfigRequest):
    """
    Update the OS brain backend config for this running simulator session.
    Secrets are forwarded in-memory only and are intentionally not logged.
    """
    shared_queues = request.app.state.SHARED_QUEUES
    if shared_queues is None:
        return JSONResponse(
            {"status": "error", "message": "Simulation not initialized"},
            status_code=503,
        )

    websocket_uri = (body.websocket_uri or "").strip() or None
    service_key = (body.service_key or "").strip() or None
    if websocket_uri and not (websocket_uri.startswith("ws://") or websocket_uri.startswith("wss://")):
        raise HTTPException(
            status_code=400,
            detail="websocket_uri must start with ws:// or wss://",
        )

    try:
        shared_queues.sim_to_agent.put_nowait(
            BrainBackendConfigCmd(
                websocket_uri=websocket_uri,
                service_key=service_key,
            )
        )
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Failed to queue brain backend update: {e}")  # noqa: B904

    status_message = "Brain backend config update queued."
    if websocket_uri:
        status_message = f"{status_message} URI will be updated."
    if service_key:
        status_message = f"{status_message} Service key provided."
    print(
        "[ConfigAPI] Brain backend config update queued "
        f"(uri={'provided' if websocket_uri else 'unchanged'}, "
        f"service_key={'provided' if service_key else 'unchanged'})"
    )
    return JSONResponse(
        {
            "status": "brain_backend_config_enqueued",
            "message": status_message,
            "websocket_uri": websocket_uri,
            "service_key_configured": service_key is not None,
        }
    )


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


# --- Moved Routes ---


@router.post("/reset_brain")
async def reset_brain(request: Request, reset_request: ResetBrainRequest | None = None):
    """Request a brain reset without resetting simulator position."""
    shared_queues = request.app.state.SHARED_QUEUES
    memory_state = reset_request.memory_state if reset_request is not None else None

    if shared_queues is None:
        return JSONResponse(
            {"status": "error", "message": "Simulation not initialized"},
            status_code=500,
        )

    try:
        reset_cmd = ResetRobotCmd(memory_state=memory_state)
        shared_queues.sim_to_agent.put_nowait(reset_cmd)
    except Exception:
        return JSONResponse({"status": "queue_full"}, status_code=503)

    return JSONResponse({"status": "reset_brain_enqueued", "memory_state": memory_state})


@router.post("/stop_agent")
def stop_agent(request: Request):
    """
    Endpoint to stop the current agent action (e.g., navigation).
    Cancels any ongoing navigation and deactivates the brain via rosbridge.

    Returns:
        JSON response confirming the agent has been stopped
    """
    shared_queues = request.app.state.SHARED_QUEUES

    # Check if we have valid shared_queues
    if shared_queues is None:
        return JSONResponse(
            {"status": "error", "message": "Simulation not initialized"},
            status_code=500,
        )

    # Cancel navigation if active
    if hasattr(shared_queues, "nav_controller") and shared_queues.nav_controller:
        shared_queues.nav_controller.cancel_navigation()

    # Deactivate brain via rosbridge (calls /brain/set_brain_active with data=False)
    try:
        shared_queues.sim_to_agent.put_nowait(BrainActiveCmd(active=False))
        shared_queues.set_brain_active(False)
        print("[ConfigAPI] Agent stopped via API endpoint (brain deactivated)")
    except Exception as e:
        print(f"[ConfigAPI] Error sending brain deactivate command: {e}")
        return JSONResponse(
            {"status": "error", "message": f"Failed to stop agent: {e}"},
            status_code=500,
        )

    return JSONResponse({"status": "success", "message": "Agent stopped"})


@router.post("/shutdown")
def shutdown_simulator(request: Request):
    """
    Endpoint to gracefully shut down the simulator.
    Sets the exit event in shared queues to signal all threads to stop.

    Returns:
        JSON response confirming shutdown has been initiated
    """
    shared_queues = request.app.state.SHARED_QUEUES

    # Check if we have valid shared_queues
    if shared_queues is None:
        return JSONResponse(
            {"status": "error", "message": "Simulation not initialized"},
            status_code=500,
        )

    # Set the exit event to signal all threads to stop
    print("[ConfigAPI] Shutdown requested via API endpoint")
    shared_queues.exit_event.set()

    return JSONResponse({"status": "success", "message": "Shutdown initiated"})
