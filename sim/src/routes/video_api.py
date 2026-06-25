import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter()

# Camera frames are streamed to the browser over WebRTC (see src/webrtc/), not
# MJPEG. The endpoints below remain for readiness/metrics polling.


@router.get("/video_feeds_ready")
def video_feeds_ready(request: Request):
    """
    Simple endpoint to check if the video feeds are ready.
    Just checks if shared_queues exists, which indicates the simulation is running.
    """
    shared_queues = request.app.state.SHARED_QUEUES

    # Simply check if shared_queues exists
    is_ready = shared_queues is not None

    return JSONResponse(
        {
            "ready": is_ready,
            "message": ("Simulation is running" if is_ready else "Simulation not initialized"),
        }
    )


@router.get("/stack_metrics")
def stack_metrics(request: Request):
    """Return lightweight simulator/runtime metrics for local stack dashboards."""
    shared_queues = request.app.state.SHARED_QUEUES
    if shared_queues is None:
        return JSONResponse(
            {
                "ready": False,
                "queue_sizes": {},
                "fps_by_camera": {},
                "latest_frame_age_by_camera": {},
                "brain_backend_status": {
                    "state": "sim_not_initialized",
                    "connected": False,
                    "message": "Simulation not initialized",
                    "updated_at": time.time(),
                    "uri": None,
                    "hosted": None,
                },
            }
        )

    metrics = shared_queues.get_runtime_metrics()
    return JSONResponse(
        {
            "ready": True,
            **metrics,
            "brain_backend_status": shared_queues.get_brain_backend_status(),
        }
    )
